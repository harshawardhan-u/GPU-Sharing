from fastapi import FastAPI, HTTPException, Request
from pydantic import BaseModel
import httpx

app = FastAPI(title="GPU Share Master Router")

# This dictionary acts as our switchboard. 
# It maps Room Codes to local Docker ports (e.g., {"123-456": 8001})
active_nodes = {}

class NodeRegistration(BaseModel):
    room_code: str
    port: int

@app.post("/api/register_node")
async def register_node(node: NodeRegistration):
    """The Flet dashboard calls this when a new container is deployed."""
    active_nodes[node.room_code] = node.port
    return {"status": "success", "message": f"Room {node.room_code} linked to port {node.port}"}

@app.delete("/api/remove_node/{room_code}")
async def remove_node(room_code: str):
    """The Flet dashboard calls this when a container is terminated."""
    if room_code in active_nodes:
        del active_nodes[room_code]
        return {"status": "success", "message": "Node disconnected"}
    raise HTTPException(status_code=404, detail="Node not found")

@app.get("/api/nodes")
async def list_nodes():
    """Quick way to see what containers are currently running."""
    return {"active_nodes": active_nodes}

# ==========================================
# THE TRAFFIC ROUTER
# ==========================================
@app.api_route("/proxy/{room_code}/{path:path}", methods=["GET", "POST", "PUT", "DELETE"])
async def proxy_to_container(room_code: str, path: str, request: Request):
    """
    When a remote user sends AI tasks to ngrok_url/proxy/123-456/predict,
    this function intercepts it and quietly forwards it to localhost:8001/predict.
    """
    if room_code not in active_nodes:
        raise HTTPException(status_code=404, detail="Invalid or inactive Room Code.")
    
    target_port = active_nodes[room_code]
    target_url = f"http://127.0.0.1:{target_port}/{path}"

    # Forward the exact request to the localized Docker container
    async with httpx.AsyncClient() as client:
        body = await request.body()
        try:
            response = await client.request(
                method=request.method,
                url=target_url,
                headers=dict(request.headers),
                content=body,
                timeout=60.0 # Give the GPU time to process!
            )
            return response.json()
        except httpx.RequestError:
            raise HTTPException(status_code=502, detail="Target GPU container is unresponsive.")