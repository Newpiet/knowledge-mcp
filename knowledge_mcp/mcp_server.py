# knowledge_mcp/mcp_server.py
"""FastMCP server exposing tools to interact with knowledge bases."""

import logging
# import asyncio # Removed unused import
import json
from textwrap import dedent
from typing import List, Optional, Any, Dict, Annotated

from pydantic import BaseModel, Field, field_validator
from fastmcp import FastMCP

# Import necessary exceptions and manager types
from knowledge_mcp.knowledgebases import KnowledgeBaseManager, KnowledgeBaseNotFoundError, KnowledgeBaseError # Added KbManager
from knowledge_mcp.rag import ConfigurationError, RAGManagerError, RagManager

logger = logging.getLogger(__name__)

# --- Helper Function ---
def _wrap_result(result: Any) -> str:
    """Simple wrapper to ensure string output, can be enhanced."""
    return str(result)

# --- Knowledge MCP Service Class ---
class MCP:
    """Encapsulates MCP tools for knowledge base interaction."""
    def __init__(self, rag_manager: RagManager, kb_manager: KnowledgeBaseManager):
        if not isinstance(rag_manager, RagManager):
            raise TypeError("Invalid RagManager instance provided")
        if not isinstance(kb_manager, KnowledgeBaseManager):
            raise TypeError("Invalid KnowledgeBaseManager instance provided")
        self.rag_manager = rag_manager
        self.kb_manager = kb_manager # Store kb_manager if needed for other tools
        self.mcp_server = FastMCP(
            name="Knowledge Base MCP",
            instructions=dedent("""
            Tools to query multiple custom knowledge bases using similarity search and a ranked knowledge-graph.
            
            Search modes explained:
            - local: Entity-specific queries - focuses on finding specific concepts, tools, or entities
            - global: Relationship discovery - focuses on understanding connections between different aspects  
            - hybrid: Cross-domain queries - combines both entity-focused and relationship-focused retrieval
            - mix: Integrates knowledge graph and vector retrieval
            - naive: Performs a basic search without advanced techniques
            """),
        )
        # Register tools using decorators
        # Register tools — keep it minimal: list, retrieve, answer
        self.mcp_server.tool(
            name="list_knowledgebases",
            description="List all available knowledge bases with metadata. Call this first to discover what knowledge is available before querying.",
        )(self.list_knowledgebases)
        self.mcp_server.tool(
            name="retrieve",
            description="Retrieve raw context passages from a knowledge base. Returns source text without LLM synthesis — useful when you need evidence for your own reasoning or want to cross-check facts. Faster than 'answer'.",
        )(self.retrieve)
        self.mcp_server.tool(
            name="answer",
            description="Query a knowledge base and get an LLM-synthesized answer with citations. One-step convenience — the server generates the answer from retrieved context. Use when you want a direct, concise answer.",
        )(self.answer)

        import os
        transport = os.environ.get("MCP_TRANSPORT", "stdio")
        host = os.environ.get("MCP_HOST", "0.0.0.0")
        port = int(os.environ.get("MCP_PORT", "8001"))
        if transport == "sse":
            logger.info(f"Starting MCP server with SSE transport on {host}:{port}")
            self.mcp_server.run(transport="sse", host=host, port=port)
        else:
            self.mcp_server.run(transport="stdio")
        logger.info("MCP service initialized.")

    async def retrieve(self,
        kb: Annotated[str, Field(description="Knowledge base directory name — use the 'kb' field from list_knowledgebases results")],
        query: Annotated[str, Field(description="Natural-language query.")],
        mode: Annotated[str, Field("mix", description='Retrieval mode ("mix", "local", "global", "hybrid", "naive", "bypass") default: "mix"')],
        top_k: Annotated[int, Field(30, ge=5, le=120, description="Number of query results to return (5-120). 30 is reasonable.")],
    ) -> str:
        """
        Retrieve raw context passages from a knowledge‑base without generating an LLM answer.
        """
        logger.info(f"Executing retrieve for KB '{kb}'")
        # Prepare kwargs for rag_manager.query
        query_kwargs = {'mode': mode, 'top_k': top_k}
        query_kwargs['only_need_context'] = True
        
        try:
            # Call the now async query method
            context_result: str = await self.rag_manager.query(
                kb_name=kb,
                query_text=query,
                **query_kwargs
            )
        except (KnowledgeBaseNotFoundError, ConfigurationError) as e:
            logger.error(f"Configuration or KB not found error during retrieve for '{kb}': {e}")
            raise ValueError(str(e)) from e # FastMCP expects ValueError for user input/config issues
        except RAGManagerError as e:
            logger.error(f"Runtime RAG error during retrieve for '{kb}': {e}", exc_info=True)
            raise RuntimeError(f"Query failed: {e}") from e # FastMCP expects RuntimeError for internal server errors
        except Exception as e:
            logger.exception(f"Unexpected error during kb_retrieve for '{kb}': {e}")
            raise RuntimeError(f"An unexpected error occurred: {e}") from e

        return _wrap_result(context_result)

    async def answer(self, 
        kb: Annotated[str, Field(description="Knowledge base directory name — use the 'kb' field from list_knowledgebases results")],
        query: Annotated[str, Field(description="Natural-language query.")],
        mode: Annotated[str, Field("mix", description='Retrieval mode ("mix", "local", "global", "hybrid", "naive", "bypass") default: "mix"')],
        top_k: Annotated[int, Field(30, ge=5, le=120, description="Number of query results to return (5-120). 30 is reasonable.")],
        response_type: Annotated[str, Field("Multiple Paragraphs", description='Answer style ("Multiple Paragraphs", "Single Paragraph", "Bullet Points").')],
    ) -> str:
        """
        Generate an LLM‑written answer using the chosen knowledge‑base and return it with citations.
        """
        logger.info(f"Executing answer for KB '{kb}'")
        # Prepare kwargs for rag_manager.query
        query_kwargs = {'mode': mode, 'top_k': top_k, 'response_type': response_type}
        query_kwargs['only_need_context'] = False

        try:
            # Call the now async query method
            answer: str = await self.rag_manager.query(
                kb_name=kb,
                query_text=query,
                **query_kwargs
            )
        except (KnowledgeBaseNotFoundError, ConfigurationError) as e:
            logger.error(f"Configuration or KB not found error during kb_answer for '{kb}': {e}")
            raise ValueError(str(e)) from e
        except RAGManagerError as e:
            logger.error(f"Runtime RAG error during kb_answer for '{kb}': {e}", exc_info=True)
            raise RuntimeError(f"Query failed: {e}") from e
        except Exception as e:
            logger.exception(f"Unexpected error during kb_answer for '{kb}': {e}")
            raise RuntimeError(f"An unexpected error occurred: {e}") from e

        return _wrap_result(answer)

    async def list_knowledgebases(self) -> str:
        """Lists all knowledge bases with rich metadata from the platform database.

        Returns name, description, document stats, and document titles to help
        the AI decide which knowledge base to query.
        """
        logger.info("Executing list_knowledgebases")
        import sqlite3
        import os

        db_path = os.path.join(os.environ.get("KB_BASE_DIR", "/app/kb"), "users.db")
        try:
            conn = sqlite3.connect(db_path, timeout=10)
            conn.row_factory = sqlite3.Row

            # Get all KBs with document stats
            kbs = conn.execute("""
                SELECT
                    kb.kb_dir_name,
                    kb.name,
                    kb.description,
                    kb.domain,
                    kb.created_at,
                    COUNT(d.id) AS total_docs,
                    SUM(CASE WHEN d.status = 'completed' THEN 1 ELSE 0 END) AS indexed_docs
                FROM knowledge_bases kb
                LEFT JOIN documents d ON d.kb_id = kb.id
                WHERE kb.status = 'active'
                GROUP BY kb.id
            """).fetchall()

            kb_list = []
            for kb in kbs:
                info: dict = {
                    "kb": kb["kb_dir_name"],
                    "name": kb["name"],
                    "description": kb["description"] or "暂无描述",
                    "domain": kb["domain"] or "",
                    "total_documents": kb["total_docs"],
                    "indexed_documents": kb["indexed_docs"] or 0,
                    "created_at": kb["created_at"],
                }

                # Layer 2: get document titles as content hints
                doc_rows = conn.execute("""
                    SELECT original_name FROM documents
                    WHERE kb_id = (SELECT id FROM knowledge_bases WHERE kb_dir_name = ?)
                      AND status = 'completed'
                    ORDER BY created_at DESC LIMIT 10
                """, (kb["kb_dir_name"],)).fetchall()
                info["document_titles"] = [r["original_name"] for r in doc_rows]

                kb_list.append(info)

            conn.close()

            result = {"knowledge_bases": kb_list, "total": len(kb_list)}
            return json.dumps(result, ensure_ascii=False)

        except sqlite3.Error as e:
            logger.warning(f"Could not read platform DB ({db_path}): {e}, falling back to filesystem")
            # Fallback: use filesystem-based listing
            try:
                kb_dict: Dict[str, str] = await self.kb_manager.list_kbs()
                kb_list = [{"kb": name, "name": name, "description": desc} for name, desc in kb_dict.items()]
                return json.dumps({"knowledge_bases": kb_list, "total": len(kb_list)}, ensure_ascii=False)
            except KnowledgeBaseError as e2:
                raise ValueError(f"Failed to list knowledge bases: {e2}") from e2
        except Exception as e:
            logger.exception(f"Unexpected error during list_knowledgebases: {e}")
            raise RuntimeError(f"An unexpected server error occurred: {e}") from e
