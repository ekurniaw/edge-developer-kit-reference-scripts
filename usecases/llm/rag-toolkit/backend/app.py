# INTEL CONFIDENTIAL
# Copyright (C) 2024, Intel Corporation

import os
os.environ['HF_HOME'] = "../data/huggingface"
os.environ['NLTK_DATA'] = "../data/nltk_data"

import sys
sys.path.append("../thirdparty/MeloTTS")

import io
import time
import uuid
import logging
import requests
import numpy as np
import multiprocessing
from contextlib import asynccontextmanager
from typing_extensions import TypedDict, Required
from typing import List, Union, Optional

from openai import OpenAI

import uvicorn
from pydantic import BaseModel
from fastapi.responses import JSONResponse, StreamingResponse, FileResponse
from fastapi.middleware.cors import CORSMiddleware
from fastapi import FastAPI, File, Form, UploadFile, HTTPException
from fastapi.encoders import jsonable_encoder
from starlette.background import BackgroundTasks

from utils.common import read_config_file
from utils.prompt import RAG_PROMPT, NO_CONTEXT_FOUND_PROMPT, QUERY_REWRITE_PROMPT
from utils.chroma_client import ChromaClient
from utils.stt import load_stt_model, inference_transcribe, inference_translate
from utils.tts import load_tts_model

logger = logging.getLogger('uvicorn.error')

OPENAI_BASE_URL = os.environ.get("OPENAI_BASE_URL", "http://localhost:8012/v1")
FEATURE_SUPPORTED = {
    "RAG": True,
    "FUNCTION_CALLING": False
}
ASR_MODEL = None
TTS_MODEL = None
TTS_SPEAKER_IDS = None
CHROMA_CLIENT = None
VECTORDB_DIR = "../data/embeddings"
DOCSTORE_DIR = "../data/embeddings/documents"
client = OpenAI(
    api_key=os.environ.get("OPENAI_API_KEY", "-"),
    base_url=OPENAI_BASE_URL
)

class TTSData(BaseModel):
    input: str
    model: str = '-'
    voice: str = 'EN-US'
    response_format: Optional[str] = 'wav'  # [mp3, opus, aac, flac, wav, pcm]
    speed: Optional[float] = 1.0  # [0.25 - 4.0]


class ICreateChatCompletions(TypedDict, total=False):
    messages: Required[List]
    model: str
    rag: bool = False
    endpoint: str
    suffix: str
    max_tokens: int = 16
    temperature: Union[int, float] = 1
    top_p: Union[int, float] = 1
    n: int = 1
    stream: bool = False
    logprobs: int
    echo: bool = False
    stop: Union[str, List[str]]
    presence_penalty: float = 0
    frequency_penalty: float = 0
    best_of: int
    logit_bias: dict[str, float]
    user: str
    top_k: int = -1
    ignore_eos: bool = False
    use_beam_search: bool = False
    stop_token_ids: List[int]
    skip_special_tokens: bool = True


@asynccontextmanager
async def lifespan(app: FastAPI):
    global CHROMA_CLIENT, ASR_MODEL, TTS_MODEL, TTS_SPEAKER_IDS
    logger.info("Initializing server services ...")
    configs = read_config_file("./config.yaml")

    CHROMA_CLIENT = ChromaClient(VECTORDB_DIR, configs["EMBEDDINGS"]["DEVICE"], configs["RERANKER"]["DEVICE"])
    ASR_MODEL = load_stt_model(model_id=configs["STT"]["MODEL_ID"], device=configs["STT"]["DEVICE"])
    
    TTS_MODEL, TTS_SPEAKER_IDS = load_tts_model(language='EN', device="cpu")
    yield
    logger.info("Stopping server services ...")


app = FastAPI(lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=['*'],
    allow_headers=['*']
)


@app.get('/healthcheck', status_code=200)
def get_healthcheck():
    return 'OK'


@app.get("/v1/models", status_code=200)
async def get_available_model():
    model_list = client.models.list()
    return model_list


# TTS Routes
@app.post("/v1/audio/speech")
async def speech_to_text(data: TTSData, bg_task: BackgroundTasks):
    global TTS_MODEL, TTS_SPEAKER_IDS

    async def remove_file(file_name):
        if os.path.exists(file_name):
            try:
                os.remove(file_name)
            except:
                logger.error(f"File: {file_name} not available")

    if TTS_MODEL == None or TTS_SPEAKER_IDS == None:
        raise HTTPException(
            status_code=403, detail="Model is not loaded. Please call the /v1/")

    output_path = f"../data/temp/tts/{str(uuid.uuid4())}.wav"

    start_time = time.time()
    TTS_MODEL.tts_to_file(
        data.input, TTS_SPEAKER_IDS[data.voice], output_path, speed=data.speed)
    elapsed_time = time.time() - start_time
    logger.info(f"TTS Inference time: {elapsed_time:.2} secs")

    if os.path.isfile(output_path):
        return FileResponse(output_path, media_type="audio/wav", background=bg_task.add_task(remove_file, output_path))
    else:
        raise HTTPException(
            status_code=500, detail="Error in getting the generated output wav.")


# STT Routes
@app.post("/v1/audio/transcriptions")
async def stt_transcription(file: UploadFile = File(...), language: Optional[str] = Form(None)):
    global ASR_MODEL
    try:
        _audio_byte = file.file.read()
        audio_byte = io.BytesIO(_audio_byte)
        text = inference_transcribe(
            model=ASR_MODEL,
            audio=audio_byte,
            language=language
        )

    except Exception as error:
        raise HTTPException(
            status_code=500, detail=f"Failed to transcribe the voice input. Error: {error}")

    return {"text": text, 'status': True}


@app.post("/v1/audio/translations")
async def stt_translation(file: UploadFile = File(...), language: Optional[str] = Form(None)):
    global ASR_MODEL
    try:
        _audio_byte = file.file.read()
        audio_byte = io.BytesIO(_audio_byte)
        text = inference_translate(
            model=ASR_MODEL,
            audio=audio_byte,
            language=language
        )

    except Exception as error:
        raise HTTPException(
            status_code=500, detail=f"Failed to translate the voice input. Error: {error}")

    return {"text": text, 'status': True}


# RAG Routes
@app.get("/v1/rag/text_embeddings", status_code=200)
async def get_text_embeddings(page: Optional[int] = 1, pageSize: Optional[int] = 5, source: Optional[str] = ""):
    global CHROMA_CLIENT
    data = CHROMA_CLIENT.get_all_collection_data(page, pageSize, source)
    result = {"status": True, "data": data}
    return JSONResponse(content=jsonable_encoder(result))


@app.get("/v1/rag/text_embedding_sources", status_code=200)
async def get_text_embedding_sources():
    global CHROMA_CLIENT
    data = CHROMA_CLIENT.get_all_sources()
    result = {"status": True, "data": data}
    return JSONResponse(content=jsonable_encoder(result))


@app.delete("/v1/rag/text_embeddings/{uuid}", status_code=200)
async def delete_text_embeddings(uuid: str):
    global CHROMA_CLIENT
    data = CHROMA_CLIENT.delete_data(uuid)
    result = {"status": True, "data": data}
    return JSONResponse(content=jsonable_encoder(result))


@app.delete("/v1/rag/text_embeddings/source/{source}", status_code=200)
async def delete_text_embeddings(source: str):
    global CHROMA_CLIENT
    source_path = f"../data/embeddings/documents/{source}"
    try:
        if os.path.isfile(source_path):
            logger.info(f"Removing the source file: {source_path}")
            os.remove(source_path)
        data = CHROMA_CLIENT.delete_data_by_source(source)
        result = {"status": True, "data": data}
    except Exception as error:
        result = {"status": False, "data": None, "detail": error}
    return JSONResponse(content=jsonable_encoder(result))


@app.post("/v1/rag/text_embeddings", status_code=200)
async def upload_rag_doc(chunk_size: int, chunk_overlap: int, files: List[UploadFile] = [UploadFile(...)]):
    global CHROMA_CLIENT
    ALLOWED_EXTENSIONS = ['.pdf', '.txt']
    file_list = []
    processed_list = []
    for file in files:
        if not file.filename.endswith(tuple(ALLOWED_EXTENSIONS)):
            logger.warning(f"{file.filename} is not the supported type.")
            continue
        else:
            file_list.append(file)

    if len(file_list) == 0:
        logger.error("No file is able to use to create text embeddings.")
        raise HTTPException(
            status_code=400, detail="No file is able to use to create text embeddings.")

    if not os.path.isdir(DOCSTORE_DIR):
        os.makedirs(DOCSTORE_DIR, exist_ok=True)

    for file in file_list:
        with open(f"{DOCSTORE_DIR}/{file.filename}", "wb") as f:
            processed_list.append(file.filename)
            f.write(file.file.read())

    CHROMA_CLIENT.create_collection_data(
        processed_list, chunk_size, chunk_overlap
    )
    result = {"status": True, "data": processed_list}
    return JSONResponse(content=jsonable_encoder(result))


# Chat completions routes
@app.post("/v1/chat/completions", status_code=200)
async def chat_completion(data: ICreateChatCompletions):
    global CHROMA_CLIENT

    def _streamer():
        for chunk in llm.iter_content(chunk_size=1024):
            yield (chunk)

    def _verify_embeddings_available():
        num_embeddings = CHROMA_CLIENT.get_num_embeddings()
        logger.info(f"Number of embeddings available: {num_embeddings}")
        if num_embeddings == 0:
            return False
        return True

    def _get_high_score_context(data, threshold):
        return [elem for elem in data if elem['score'] > threshold]

    def _formatting_rag_fusion_result(retrieval_list, query):
        context = ""
        for i, item in enumerate(retrieval_list):
            document = item['document']
            score = np.array(item['score']) * 100
            context += f"Context {i+1}: {document}.\nScore: {score:.2}."
            if i < len(retrieval_list) - 1:
                context += "\n\n"

        formatted_prompt = RAG_PROMPT.format(
            context=context,
            question=query
        )
        return formatted_prompt

    isRAG = False

    if 'rag' in data:
        isRAG = data['rag']

    if isRAG:
        logger.info("RAG settings is enabled. Verifying embedding is available")
        isEmbeddings = _verify_embeddings_available()
        if isEmbeddings:
            last_user_message = data['messages'][-1]['content']
            logger.info("Setting the temperature to 0.01 for RAG")
            data['temperature'] = 0.01

            reranker_results = CHROMA_CLIENT.query_data(last_user_message)
            if len(reranker_results) == 0:
                logger.error(
                    "Failed to retrieve contexts for RAG. Default to use normal message without RAG")
            else:
                filtered_results = _get_high_score_context(reranker_results, 0)
                if len(filtered_results) > 0:
                    logger.info("Successfully retrieve contexts for RAG")
                    user_prompt = _formatting_rag_fusion_result(
                        filtered_results, last_user_message)
                else:
                    logger.info("Failed to retrieve contexts for RAG")
                    user_prompt = NO_CONTEXT_FOUND_PROMPT.format(
                        question=last_user_message)
                data['messages'][-1]['content'] = user_prompt

    endpoint = data.get(
        'endpoint', f"{OPENAI_BASE_URL}/chat/completions")

    if 'rag' in data:
        data.pop('rag')

    try:
        llm = requests.post(
            endpoint,
            json=data,
            stream=True
        )
    except Exception as error:
        logger.error(
            f"Error while proxying to serving services. Error: {error}")
        raise HTTPException(
            status_code=400, detail="Error while proxying to serving services")

    return StreamingResponse(_streamer(), media_type="text/event-stream")


if __name__ == "__main__":
    multiprocessing.freeze_support()
    uvicorn.run(
        app,
        host=os.environ.get('SERVER_HOST', "0.0.0.0"),
        port=int(os.environ.get('SERVER_PORT', "8011"))
    )
