import os
from typing import Dict, List, AsyncGenerator, Optional
import uvicorn
from fastapi import FastAPI, HTTPException, status, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from dotenv import load_dotenv
import json

load_dotenv()
import logging
from src.interface.agent import *
from src.workflow.process import run_agent_workflow
from src.manager import agent_manager 
from src.manager.agents import NotFoundAgentError
from src.service.session import SessionManager
from src.interface.agent import RemoveAgentRequest
from src.interface.workflow import WorkflowRequest
from fastapi.responses import FileResponse
from src.service.tool_tracker import tool_tracker
from src.tools.websocket_manager import websocket_manager
from src.workflow.cache import workflow_cache


logger = logging.getLogger(__name__)
logger.setLevel(logging.WARNING)
session_manager = SessionManager()
app = FastAPI()
app.add_middleware(
            CORSMiddleware, allow_origins=["*"], allow_credentials=True, allow_methods=["*"], allow_headers=["*"]
        )
class Server:
    def __init__(self, host="0.0.0.0", port=8001) -> None:
        self.host = host
        self.port = port

    def _process_request(self, request: "AgentRequest") -> List[Dict[str, str]]:
        return [{"role": message.role, "content": message.content} for message in request.messages]

    @staticmethod
    async def _run_agent_workflow(
            request: "AgentRequest"
    ) -> AsyncGenerator[str, None]:
        if agent_manager is None:
             logger.error("Agent workflow called before AgentManager was initialized.")
             raise HTTPException(status_code=503, detail="Service not ready, AgentManager not initialized.")

        session = session_manager.get_session(request.user_id)
        for message in request.messages:
            session.add_message(message.role, message.content)
        session_messages = session.history[-3:]

        response_stream = run_agent_workflow(
            user_id=request.user_id,
            task_type=request.task_type,
            user_input_messages=session_messages,
            debug=request.debug,
            deep_thinking_mode=request.deep_thinking_mode,
            search_before_planning=request.search_before_planning,
            coor_agents=request.coor_agents,
            workmode=request.workmode,
            workflow_id=request.workflow_id,
        )
        async for res in response_stream:
            try:
                event_type = res.get("event")
                # Handle new_agent_created event (agent_obj is already an Agent instance)
                if event_type == "new_agent_created" and "data" in res and "agent_obj" in res["data"]:
                    agent_obj = res["data"]["agent_obj"]
                    # Serialize the Agent object to JSON string if needed by client
                    agent_json = agent_obj.model_dump_json(indent=2) if agent_obj else None
                    if agent_json:
                        res["data"]["agent_obj"] = agent_json # Replace object with JSON string
                    else:
                        # Handle case where agent object is missing or serialization fails
                        logger.warning("Could not serialize agent object for new_agent_created event.")
                        # Optionally remove the agent_obj key or the entire event
                        if "agent_obj" in res["data"]: del res["data"]["agent_obj"]

                # This yield should be outside the if/else block for serialization
                yield res
            except (TypeError, ValueError, json.JSONDecodeError) as e:
                from traceback import print_stack
                print_stack()
                logging.error(f"Error serializing event: {e}", exc_info=True)
                
    @staticmethod
    async def _list_agents(
         request: "listAgentRequest"
    ) -> AsyncGenerator[str, None]:
        if agent_manager is None:
             raise HTTPException(status_code=503, detail="Service not ready, AgentManager not initialized.")
        try:
            agents = await agent_manager._list_agents(request.user_id, request.match)
            for agent in agents:
                yield agent.model_dump_json() + "\n"
        except Exception as e:
            logger.error(f"Error listing agents: {e}", exc_info=True)
            raise HTTPException(status_code=500, detail=str(e))

    @staticmethod
    async def _list_agents_json(user_id: str, match: Optional[str] = None):
        try:
            agents = await agent_manager._list_agents(user_id, match)
            return [agent.model_dump() for agent in agents]
        except Exception as e:
            raise HTTPException(status_code=500, detail=str(e))

    @staticmethod
    async def _workflow_draft(user_id: str, match: str):
        try:
            workflows = workflow_cache.list_workflows(user_id, match)
            return workflows[0]
        except Exception as e:
            raise HTTPException(status_code=500, detail=str(e))
    @staticmethod
    async def _list_workflow_json(user_id: str, match: Optional[str] = None):
        try:
            workflows = workflow_cache.list_workflows(user_id, match)
            default_workflows = workflow_cache.list_workflows('share')
            workflows.extend(default_workflows)
            workflowJsons = []
            for workflow in workflows:
                workflowJsons.append({
                    "workflow_id": workflow["workflow_id"],
                    "version": workflow["version"],
                    "lap": workflow["lap"],
                    "user_input_messages": workflow["user_input_messages"],
                    "deep_thinking_mode": workflow["deep_thinking_mode"],
                    "search_before_planning": workflow["search_before_planning"]
                })
            return [workflowJson for workflowJson in workflowJsons]
        except Exception as e:
            raise HTTPException(status_code=500, detail=str(e))
    @staticmethod
    def _list_workflow(
         request: "listAgentRequest"
    ) -> AsyncGenerator[str, None]:
        if workflow_cache is None:
             raise HTTPException(status_code=503, detail="Service not ready, WorkflowCache not initialized.")
        try:
            workflows = workflow_cache.list_workflows(request.user_id, request.match)
            return workflows
        except Exception as e:
            logger.error(f"Error listing workflows: {e}", exc_info=True)
            raise HTTPException(status_code=500, detail=str(e))

    @staticmethod
    async def _list_user_all_agents(user_id: str):
        try:
            agents = agent_manager._list_user_all_agents(user_id)
            return [agent.model_dump() for agent in agents]
        except Exception as e:
            raise HTTPException(status_code=500, detail=str(e))
    @staticmethod
    async def _list_default_agents_json():
        try:
            agents = agent_manager._list_default_agents()
            return [agent.model_dump() for agent in agents]
        except Exception as e:
            raise HTTPException(status_code=500, detail=str(e))
    @staticmethod
    async def _edit_workflow(user_id: str, workflow):
        try:
            nodes = workflow["nodes"]
            for key, node in nodes.items():
                if node["component_type"] == "agent" and node["config"]["type"] == "execution_agent":
                    if "add" in node and node["add"] == "1":
                        agent_name = node["name"]
                        agents = agent_manager._list_user_all_agents(user_id)
                        for agent in agents:
                            if agent["agent_name"] == node["name"]:
                                from datetime import datetime
                                timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
                                agent_name = f"{node['name']}_{timestamp}"
                                break
                        _tools = []
                        for tool in node["config"]["tools"]:
                            _tools.append(Tool(
                                name=tool["config"]["name"],
                                description=tool["config"]["description"],
                            ))
                        agent = Agent(
                            user_id=user_id,
                            agent_name=agent_name,
                            nick_name=node["label"],
                            description=node["config"]["description"],
                            llm_type=node["config"]["llm_type"],
                            selected_tools=_tools,
                            prompt=node["config"]["prompt"]
                        )
                        agent_manager._save_agent(agent, flush=True)
                        for graph in workflow["graph"]:
                            if graph["component_type"] == "agent" and graph["config"]["node_type"] == "execution_agent":
                                if graph["name"] == node["name"]:
                                    graph["name"] = agent_name
                                    break
                        workflow_cache.save_workflow(user_id, workflow)
                    else:
                        _tools = []
                        for tool in node["config"]["tools"]:
                            _tools.append(Tool(
                                name=tool["config"]["name"],
                                description=tool["config"]["description"],
                            ))
                        agent = Agent(
                            user_id=user_id,
                            agent_name=node["name"],
                            nick_name=node["label"],
                            description=node["config"]["description"],
                            llm_type=node["config"]["llm_type"],
                            selected_tools=_tools,
                            prompt=node["config"]["prompt"]
                        )
                        agent_manager._edit_agent(agent)
                        workflow_cache.save_workflow(user_id, workflow)
            return workflow
        except Exception as e:
            raise HTTPException(status_code=500, detail=str(e))

    @staticmethod
    async def _list_default_agents() -> AsyncGenerator[str, None]:
        if agent_manager is None:
             raise HTTPException(status_code=503, detail="Service not ready, AgentManager not initialized.")
        try:
            agents = await agent_manager._list_default_agents()
            for agent in agents:
                yield agent.model_dump_json() + "\n"
        except Exception as e:
            logger.error(f"Error listing default agents: {e}", exc_info=True)
            raise HTTPException(status_code=500, detail=str(e))

    @staticmethod
    async def _list_default_tools() -> AsyncGenerator[str, None]:
        if agent_manager is None:
             raise HTTPException(status_code=503, detail="Service not ready, AgentManager not initialized.")
        try:
            tools = await agent_manager._list_default_tools()
            for tool in tools:
                yield tool.model_dump_json() + "\n"
        except Exception as e:
            logger.error(f"Error listing default tools: {e}", exc_info=True)
            raise HTTPException(status_code=500, detail=str(e))

    @staticmethod
    async def _edit_agent(
        request: "Agent"
    ) -> AsyncGenerator[str, None]:
        if agent_manager is None:
             raise HTTPException(status_code=503, detail="Service not ready, AgentManager not initialized.")
        try:
            result = agent_manager._edit_agent(request)
            yield json.dumps({"result": result}) + "\n"
        except NotFoundAgentError as e:
            logger.warning(f"Edit agent failed: {e}")
            yield json.dumps({"result": "agent not found", "error": str(e)}) + "\n"
        except Exception as e:
            logger.error(f"Error editing agent {request.agent_name}: {e}", exc_info=True)
            yield json.dumps({"result": "error", "error": str(e)}) + "\n"

    @staticmethod
    async def _edit_planning_steps(
        request: "EditStepsRequest"
    ) -> AsyncGenerator[str, None]:
        if agent_manager is None:
             raise HTTPException(status_code=503, detail="Service not ready, AgentManager not initialized.")
        try:
            workflow_cache.save_planning_steps(request.workflow_id,request.planning_steps)
            yield json.dumps({"result": "success"}) + "\n"
        except Exception as e:
            logger.error(f"Error editing planning steps : {e}", exc_info=True)
            yield json.dumps({"result": "error", "error": str(e)}) + "\n"

    @staticmethod
    async def _remove_agent(request: RemoveAgentRequest) -> AsyncGenerator[str, None]:
        if agent_manager is None:
             yield json.dumps({"result": "error", "message": "Service not ready, AgentManager not initialized."}) + "\n"
             return
        try:
            await agent_manager._remove_agent(request.agent_name)
            yield json.dumps({"result": "success", "message": f"Agent '{request.agent_name}' deleted successfully."}) + "\n"
        except Exception as e:
            logger.error(f"Error removing agent {request.agent_name}: {e}", exc_info=True)
            yield json.dumps({"result": "error", "message": f"Error removing Agent '{request.agent_name}': {str(e)}"}) + "\n"

    @staticmethod
    async def _get_browser_container_info(user_id: str):
        """Get user's browser container information"""
        try:
            # Currently returns fixed container address, can be dynamically allocated based on user_id in the future
            container_info = {
                "user_id": user_id,
                "container_url": "http://106.13.116.188:30005",
                "status": "active",
                "message": "Browser container is ready"
            }
            return container_info
        except Exception as e:
            logger.error(f"Error getting browser container info for user {user_id}: {e}", exc_info=True)
            raise HTTPException(status_code=500, detail=str(e))

    @staticmethod
    async def _get_active_tools(user_id: str):
        """Get tools currently being used by the user"""
        try:
            active_tools = tool_tracker.get_active_tools(user_id)
            
            # Check if browser tool is being used
            is_browser_active = "browser" in active_tools
            
            # Get last usage time of browser tool
            browser_timestamp = None
            if user_id in tool_tracker._user_tool_usage and "browser" in tool_tracker._user_tool_usage[user_id]:
                browser_timestamp = tool_tracker._user_tool_usage[user_id]["browser"].isoformat()
            
            result = {
                "user_id": user_id,
                "active_tools": list(active_tools),
                "browser_active": is_browser_active,
                "browser_last_used": browser_timestamp
            }
            
            return result
        except Exception as e:
            logger.error(f"Error getting active tools for user {user_id}: {e}", exc_info=True)
            raise HTTPException(status_code=500, detail=str(e))

    def launch(self):
        uvicorn.run(
            "app:app",
            host=self.host,
            port=self.port,
            workers=1
        )


@app.post("/v1/save_workflow", status_code=status.HTTP_200_OK)
async def save_workflow_endpoint(request: WorkflowRequest):
    try:
        workflow = json.loads(request.data)
        await Server._edit_workflow(request.user_id, workflow)
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="Invalid JSON string")
    except Exception as e:
        logger.error(f"Error saving workflow: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/v1/workflow", status_code=status.HTTP_200_OK)
async def agent_workflow_endpoint(request: AgentRequest):
    async def response_generator():
        async for chunk in Server._run_agent_workflow(request):
            yield json.dumps(chunk, ensure_ascii=False) + "\n"

    return StreamingResponse(
        response_generator(),
        media_type="application/x-ndjson"
    )


@app.get("/v1/list_workflow_json", status_code=status.HTTP_200_OK)
async def list_workflow_json(user_id: str, match: Optional[str] = None):
    try:
        workflows = await Server._list_workflow_json(user_id, match)
        return workflows
    except HTTPException as e:
        raise e


@app.get("/v1/workflow_draft", status_code=status.HTTP_200_OK)
async def workflow_draft(user_id: str, match: str):
    try:
        workflow = await Server._workflow_draft(user_id, match)
        return workflow
    except HTTPException as e:
        raise e


@app.post("/v1/list_agents", status_code=status.HTTP_200_OK)
async def list_agents_endpoint(request: listAgentRequest):
    return StreamingResponse(
        Server._list_agents(request),
        media_type="application/x-ndjson"
    )


@app.get("/get_image/{image_name}")
async def get_image(image_name: str):
    root_dir = os.getcwd()  # Get current working directory, which is the root directory
    image_path = os.path.join(root_dir, image_name)

    if not os.path.isfile(image_path):
        raise HTTPException(status_code=404, detail="Image not found")

    return FileResponse(image_path)


@app.get("/v1/list_agents_json", status_code=status.HTTP_200_OK)
async def list_agents_json(user_id: str, match: Optional[str] = None):
    try:
        agents = await Server._list_agents_json(user_id, match)
        return agents
    except HTTPException as e:
        raise e


@app.get("/v1/list_user_all_agents", status_code=status.HTTP_200_OK)
async def list_user_all_agents(user_id: str):
    try:
        agents = await Server._list_user_all_agents(user_id)
        return agents
    except HTTPException as e:
        raise e


@app.get("/v1/list_default_agents", status_code=status.HTTP_200_OK)
async def list_default_agents_endpoint():
    return StreamingResponse(
        Server._list_default_agents(),
        media_type="application/x-ndjson"
    )


@app.get("/v1/list_default_agents_json", status_code=status.HTTP_200_OK)
async def list_default_agents_json():
    try:
        agents = await Server._list_default_agents_json()
        return agents
    except HTTPException as e:
        raise e


@app.get("/v1/list_default_tools", status_code=status.HTTP_200_OK)
async def list_default_tools_endpoint():
    return StreamingResponse(
        Server._list_default_tools(),
        media_type="application/x-ndjson"
    )


@app.post("/v1/edit_agent", status_code=status.HTTP_200_OK)
async def edit_agent_endpoint(request: Agent):
    return StreamingResponse(
        Server._edit_agent(request),
        media_type="application/x-ndjson"
    )


@app.post("/v1/edit_planning_steps", status_code=status.HTTP_200_OK)
async def edit_planning_steps_endpoint(request: EditStepsRequest):
    return StreamingResponse(
        Server._edit_planning_steps(request),
        media_type="application/x-ndjson"
    )


@app.post("/v1/remove_agent", status_code=status.HTTP_200_OK)
async def remove_agent_endpoint(request: RemoveAgentRequest):
    return StreamingResponse(
        Server._remove_agent(request),
        media_type="application/x-ndjson"
    )


@app.get("/v1/browser_container/{user_id}", status_code=status.HTTP_200_OK)
async def get_browser_container_info(user_id: str):
    """Get user's browser container information"""
    try:
        container_info = await Server._get_browser_container_info(user_id)
        return container_info
    except HTTPException as e:
        raise e


@app.get("/v1/active_tools/{user_id}", status_code=status.HTTP_200_OK)
async def get_active_tools(user_id: str):
    """Get tools currently being used by the user"""
    try:
        active_tools_info = await Server._get_active_tools(user_id)
        return active_tools_info
    except HTTPException as e:
        raise e


@app.websocket("/ws/tools/{user_id}")
async def websocket_endpoint(websocket: WebSocket, user_id: str):
    """WebSocket endpoint for real-time tool usage notifications"""
    await websocket_manager.connect(websocket, user_id)

    try:
        # Keep connection alive
        while True:
            try:
                # Wait for client messages (heartbeat, etc.)
                data = await websocket.receive_text()
                # Simply ignore client messages, just keep connection alive
            except WebSocketDisconnect:
                break
            except Exception as e:
                logger.error(f"Error handling WebSocket message: {e}")
                break

    except WebSocketDisconnect:
        pass
    except Exception as e:
        logger.error(f"WebSocket connection error: {e}")
    finally:
        await websocket_manager.disconnect(websocket, user_id)


def parse_arguments():
    import argparse
    parser = argparse.ArgumentParser(description="Agent Server API")
    parser.add_argument("--host", default="0.0.0.0", type=str, help="Service host")
    parser.add_argument("--port", default=8001, type=int, help="Service port")
    
    return parser.parse_args()


if __name__ == "__main__":

    args = parse_arguments()

    server = Server(host=args.host, port=args.port)
    server.launch()
