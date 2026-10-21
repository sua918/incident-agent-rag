"""Vue 채팅 화면을 제공하고 백엔드 API를 같은 출처로 중계한다.

기본 실행 주소는 http://127.0.0.1:8630 이다.
백엔드는 BACKEND_URL 설정에 따라 연결한다.
"""
import os
from pathlib import Path
from dotenv import load_dotenv

import httpx
import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse

load_dotenv(Path(__file__).parent / ".env")
BACKEND = os.getenv("BACKEND_URL", "http://127.0.0.1:8130")
HERE = Path(__file__).parent

app = FastAPI()


@app.get("/")
async def index():
    return FileResponse(HERE / "web_chat.html")


@app.get("/health")
async def proxy_health():
    async with httpx.AsyncClient(timeout=10.0) as client:
        r = await client.get(f"{BACKEND}/health")
    return JSONResponse(r.json(), status_code=r.status_code)


@app.post("/chat")
async def proxy_chat(req: Request):
    body = await req.body()
    async with httpx.AsyncClient(timeout=120.0) as client:
        r = await client.post(f"{BACKEND}/chat", content=body,
                              headers={"content-type": "application/json"})
    return JSONResponse(r.json(), status_code=r.status_code)


@app.get("/chat/stream")
async def proxy_stream(message: str, thread_id: str = "web-demo"):
    async def relay():
        async with httpx.AsyncClient(timeout=None) as client:
            async with client.stream(
                "GET", f"{BACKEND}/chat/stream",
                params={"message": message, "thread_id": thread_id},
            ) as upstream:
                async for chunk in upstream.aiter_bytes():
                    yield chunk

    return StreamingResponse(relay(), media_type="text/event-stream")


@app.get("/diagnoses")
async def diagnoses(q: str = "", status: str = ""):
    async with httpx.AsyncClient(timeout=10) as client:
        response = await client.get(BACKEND + "/diagnoses", params={"q": q, "status": status})
    return JSONResponse(response.json(), status_code=response.status_code)


@app.get("/diagnoses/{diagnosis_id}")
async def diagnosis(diagnosis_id: str):
    async with httpx.AsyncClient(timeout=10) as client:
        response = await client.get(BACKEND + "/diagnoses/" + diagnosis_id)
    return JSONResponse(response.json(), status_code=response.status_code)


if __name__ == "__main__":
    print("브라우저에서 http://127.0.0.1:8630 을 열어보세요.")
    uvicorn.run(app, host="127.0.0.1", port=int(os.getenv("FRONTEND_PORT", "8630")))
