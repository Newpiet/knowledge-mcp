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
        self.mcp_server.tool(name="retrieve", description="Returns the retrieval results only. Good when the client AI needs evidence for its own chain-of-thought or wish to cross-check multiple modes/top-k values cheaply. Retrieves raw context passages from a knowledge base without synthesizing an LLM answer. Client AI must generate the answer and that increases token volume for the client AI. Faster response, good for multiple queries.")(self.retrieve)
        self.mcp_server.tool(name="answer", description="Returns an LLM-synthesised answer from the retrieval results. Good when you want a concise answer in one call. Uses the LLM of the mcp server to generate an answer from a knowledge base and return it with citations. Server AI must generate the answer and that increases token volume for this LLM.")(self.answer)
        self.mcp_server.tool(name="list_knowledgebases", description="List all available knowledge bases.")(self.list_knowledgebases)
        self.mcp_server.tool(name="query_local", description="Simplified local mode query - Best for entity-specific queries. Focuses on finding specific concepts, tools, or entities within your domains.")(self.query_local)
        self.mcp_server.tool(name="query_global", description="Simplified global mode query - Best for relationship discovery. Focuses on understanding relationships and connections between different aspects of your domains.")(self.query_global)
        self.mcp_server.tool(name="query_hybrid", description="Simplified hybrid mode query - Best for cross-domain queries. Combines both entity-focused and relationship-focused retrieval, ideal for comprehensive coverage spanning multiple knowledge areas.")(self.query_hybrid)
        self.mcp_server.run(transport="stdio")
        logger.info("MCP service initialized.")

    async def retrieve(self,
        kb: Annotated[str, Field(description="Knowledge base to query")],
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
        kb: Annotated[str, Field(description="Knowledge base to query")],
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

    async def query_local(self,
        kb: Annotated[str, Field(description="Knowledge base to query")],
        query: Annotated[str, Field(description="Natural-language query.")],
    ) -> str:
        """
        Simplified local mode query - Best for entity-specific queries.
        
        Local mode focuses on entity-centric retrieval and is excellent for questions like:
        - "What are the specific network security tools mentioned?"
        - "Which processes are relevant to vulnerability management?"
        
        Uses optimized defaults: mode='local', top_k=30, LLM generation, multiple paragraphs.
        """
        logger.info(f"Executing query_local for KB '{kb}'")
        
        # Fixed optimal parameters for local mode
        query_kwargs = {
            'mode': 'local',
            'top_k': 30,
            'response_type': 'Multiple Paragraphs',
            'only_need_context': False
        }
        
        try:
            answer: str = await self.rag_manager.query(
                kb_name=kb,
                query_text=query,
                **query_kwargs
            )
        except (KnowledgeBaseNotFoundError, ConfigurationError) as e:
            logger.error(f"Configuration or KB not found error during query_local for '{kb}': {e}")
            raise ValueError(str(e)) from e
        except RAGManagerError as e:
            logger.error(f"Runtime RAG error during query_local for '{kb}': {e}", exc_info=True)
            raise RuntimeError(f"Query failed: {e}") from e
        except Exception as e:
            logger.exception(f"Unexpected error during query_local for '{kb}': {e}")
            raise RuntimeError(f"An unexpected error occurred: {e}") from e

        return _wrap_result(answer)

    async def query_global(self,
        kb: Annotated[str, Field(description="Knowledge base to query")],
        query: Annotated[str, Field(description="Natural-language query.")],
    ) -> str:
        """
        Simplified global mode query - Best for relationship discovery.
        
        Global mode focuses on relationship-centric retrieval and is valuable for queries like:
        - "How do network configuration practices relate to security processes?"
        - "What are the dependencies between different security procedures?"
        
        Uses optimized defaults: mode='global', top_k=30, LLM generation, multiple paragraphs.
        """
        logger.info(f"Executing query_global for KB '{kb}'")
        
        # Fixed optimal parameters for global mode
        query_kwargs = {
            'mode': 'global',
            'top_k': 30,
            'response_type': 'Multiple Paragraphs',
            'only_need_context': False
        }
        
        try:
            answer: str = await self.rag_manager.query(
                kb_name=kb,
                query_text=query,
                **query_kwargs
            )
        except (KnowledgeBaseNotFoundError, ConfigurationError) as e:
            logger.error(f"Configuration or KB not found error during query_global for '{kb}': {e}")
            raise ValueError(str(e)) from e
        except RAGManagerError as e:
            logger.error(f"Runtime RAG error during query_global for '{kb}': {e}", exc_info=True)
            raise RuntimeError(f"Query failed: {e}") from e
        except Exception as e:
            logger.exception(f"Unexpected error during query_global for '{kb}': {e}")
            raise RuntimeError(f"An unexpected error occurred: {e}") from e

        return _wrap_result(answer)

    async def query_hybrid(self,
        kb: Annotated[str, Field(description="Knowledge base to query")],
        query: Annotated[str, Field(description="Natural-language query.")],
    ) -> str:
        """
        Simplified hybrid mode query - Best for cross-domain queries.
        
        Hybrid mode combines both entity-focused (local) and relationship-focused (global) retrieval,
        making it ideal for comprehensive coverage that spans multiple knowledge areas. Perfect for:
        - Cyber security processes and network configuration queries
        - Questions requiring both process knowledge and technical implementation details
        
        Uses optimized defaults: mode='hybrid', top_k=30, LLM generation, multiple paragraphs.
        """
        logger.info(f"Executing query_hybrid for KB '{kb}'")
        
        # Fixed optimal parameters for hybrid mode
        query_kwargs = {
            'mode': 'hybrid',
            'top_k': 30,
            'response_type': 'Multiple Paragraphs',
            'only_need_context': False
        }
        
        try:
            answer: str = await self.rag_manager.query(
                kb_name=kb,
                query_text=query,
                **query_kwargs
            )
        except (KnowledgeBaseNotFoundError, ConfigurationError) as e:
            logger.error(f"Configuration or KB not found error during query_hybrid for '{kb}': {e}")
            raise ValueError(str(e)) from e
        except RAGManagerError as e:
            logger.error(f"Runtime RAG error during query_hybrid for '{kb}': {e}", exc_info=True)
            raise RuntimeError(f"Query failed: {e}") from e
        except Exception as e:
            logger.exception(f"Unexpected error during query_hybrid for '{kb}': {e}")
            raise RuntimeError(f"An unexpected error occurred: {e}") from e

        return _wrap_result(answer)

    async def list_knowledgebases(self) -> str:
        """Lists all available knowledge bases and their descriptions."""
        logger.info("Executing list_knowledgebases")
        try:
            # kb_manager.list_kbs is now async and returns Dict[str, str]
            kb_dict: Dict[str, str] = await self.kb_manager.list_kbs()

            # Transform the dict into the desired list of objects format
            kb_list_formatted = [
                {"name": name, "description": description}
                for name, description in kb_dict.items()
            ]

            # Wrap in the final structure and return as JSON
            result = {"knowledge_bases": kb_list_formatted}
            return json.dumps(result)

        except KnowledgeBaseError as e:
            logger.error(f"Error listing knowledge bases: {e}", exc_info=True)
            # Use ValueError for user-facing errors expected by FastMCP
            raise ValueError(f"Failed to list knowledge bases: {e}") from e
        except Exception as e:
            logger.exception(f"Unexpected error during list_knowledgebases: {e}")
            # Use RuntimeError for internal server errors expected by FastMCP
            raise RuntimeError(f"An unexpected server error occurred: {e}") from e
