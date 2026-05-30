import json
import numpy as np
from typing import List
from sqlmodel import select
from nexus_ai_agent.storage.db import get_session
from nexus_ai_agent.storage.models import DocumentChunk

class RAGEngine:
    def __init__(self, gemini_engine=None):
        self.gemini = gemini_engine

    async def ingest_pdf(self, user_id: int, file_bytes: bytes, filename: str) -> str:
        import PyPDF2
        import io
        
        reader = PyPDF2.PdfReader(io.BytesIO(file_bytes))
        text = ""
        for page in reader.pages:
            text += page.extract_text() + "\n"
        
        # Simple chunking by words
        words = text.split()
        chunks = [" ".join(words[i:i+500]) for i in range(0, len(words), 500)]
        
        async with get_session() as session:
            for i, chunk_text in enumerate(chunks):
                # In a real scenario, we'd get embeddings from Gemini
                # For this implementation, we'll mock embeddings with random data if gemini is missing
                embedding = [0.1] * 768 
                if self.gemini:
                    # Mock embedding call
                    pass
                
                chunk = DocumentChunk(
                    user_id=user_id,
                    filename=filename,
                    chunk_text=chunk_text,
                    chunk_index=i,
                    embedding=json.dumps(embedding)
                )
                session.add(chunk)
            await session.commit()
        
        return f"Successfully ingested {len(chunks)} chunks from {filename}"

    async def query(self, user_id: int, question: str) -> str:
        async with get_session() as session:
            stmt = select(DocumentChunk).where(DocumentChunk.user_id == user_id)
            chunks = (await session.exec(stmt)).all()
            
            if not chunks:
                return "No documents found for this user."
            
            # Simple cosine similarity mock
            # In real RAG, we'd compare question embedding with chunk embeddings
            best_chunk = chunks[0].chunk_text
            
            return f"Based on your documents: {best_chunk[:500]}..."
