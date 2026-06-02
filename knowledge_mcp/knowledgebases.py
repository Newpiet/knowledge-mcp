"""Manages knowledge bases, including creation, loading, and querying."""

import logging
from pathlib import Path
from typing import Any, Dict, Optional
import shutil  # Import shutil for rmtree
import yaml  # Added for YAML loading
import asyncio # Add asyncio

from knowledge_mcp.config import Config # Import Config

logger = logging.getLogger(__name__)

# Default query parameters for a new/unconfigured knowledge base config.yaml
# Updated for lightrag-hku 1.4.0 QueryParam structure
DEFAULT_QUERY_PARAMS: dict[str, Any] = {
    "description": "A useful knowledge base", # Add description field
    "mode": "hybrid",  # Keeping hybrid as preferred default
    "only_need_context": False,
    "only_need_prompt": False,
    "response_type": "Multiple Paragraphs",
    "stream": False,  # New parameter in 1.4.0
    "top_k": 40,
    "chunk_top_k": None,  # New parameter - defaults to top_k if None
    "max_entity_tokens": 4000,  # Replaces max_token_for_text_unit
    "max_relation_tokens": 4000,  # Replaces max_token_for_global_context
    "max_total_tokens": 8000,  # Replaces max_token_for_local_context
    "hl_keywords": [],  # New parameter - high-level keywords
    "ll_keywords": [],  # New parameter - low-level keywords
    "conversation_history": [],  # New parameter - conversation history
    "history_turns": 3,
    "model_func": None,  # New parameter - optional LLM model function override
    "user_prompt": "",  # User-configurable prompt for LLM response formatting
    "enable_rerank": True,  # New parameter - enable reranking for text chunks
}

class KnowledgeBaseError(Exception):
    """Base exception for knowledge base operations."""


class KnowledgeBaseExistsError(KnowledgeBaseError):
    """Raised when trying to create a knowledge base that already exists."""


class KnowledgeBaseNotFoundError(KnowledgeBaseError):
    """Raised when trying to operate on a knowledge base that does not exist."""


class KnowledgeBaseManager:
    """Manages knowledge base directories."""

    def __init__(self, config: Config) -> None: # Accept Config object
        """
        Initializes the KnowledgeBaseManager.
        The base directory for knowledge bases is retrieved from the Config object.

        Args:
            config: The application config object.

        Raises:
            TypeError: If the config argument is not a Config instance or
                       if the resolved base directory path is not valid or accessible.
            KnowledgeBaseError: If the base directory cannot be created or is not a directory.
        """
        # Explicitly check the type of the config object first
        if not isinstance(config, Config):
            raise TypeError(f"Expected a Config instance, but got {type(config).__name__}")

        if not config.knowledge_base or not config.knowledge_base.base_dir:
            msg = "Knowledge base base_dir not configured in config."
            logger.error(msg)
            raise ValueError(msg)

        resolved_base_dir: Path = config.knowledge_base.base_dir.resolve()
        logger.info(f"Using base directory from config: {resolved_base_dir}")

        # Ensure the base directory exists
        try:
            resolved_base_dir.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            logger.error(f"Failed to create or access base directory {resolved_base_dir}: {e}")
            raise TypeError(f"Invalid base directory path or permissions: {resolved_base_dir}") from e

        self.base_dir: Path = resolved_base_dir
        logger.debug(f"KnowledgeBaseManager initialized with base_dir: {self.base_dir}")
        self.config = config

    def get_kb_path(self, name: str) -> Path:
        """Returns the full path for a given knowledge base name."""
        return self.base_dir / name

    def kb_exists(self, name: str) -> bool:
        """Checks if a knowledge base directory exists."""
        return self.get_kb_path(name).is_dir()

    def create_kb(self, name: str, description: Optional[str] = None) -> Path:
        """Creates a new knowledge base directory.

        Args:
            name: The name for the new knowledge base.
            description: An optional description for the knowledge base.

        Returns:
            The Path object pointing to the created knowledge base directory.
        """
        kb_path = self.get_kb_path(name)
        if kb_path.exists():
            raise KnowledgeBaseExistsError(f"Knowledge base '{name}' already exists at {kb_path}")

        try:
            kb_path.mkdir(parents=True, exist_ok=False)
            logger.info(f"Created knowledge base directory: {kb_path}")

            # --- Add config.yaml creation --- #
            config_data = DEFAULT_QUERY_PARAMS.copy() # Start with defaults
            if description:
                config_data["description"] = description # Override if provided

            config_file_path = kb_path / "config.yaml"
            try:
                with open(config_file_path, 'w', encoding='utf-8') as f:
                    yaml.dump(config_data, f, default_flow_style=False) # Use config_data
                logger.info(f"Created default config file: {config_file_path}")
            except (IOError, yaml.YAMLError) as e:
                logger.error(f"Failed to create default config file for KB '{name}': {e}")
                # Consider if we should clean up the created directory here
                # For now, we'll raise an error indicating partial success/failure
                raise KnowledgeBaseError(f"KB directory created, but failed to write config.yaml: {e}") from e
            # --- End config.yaml creation --- #

            return kb_path
        except OSError as e:
            logger.error(f"Failed to create directory for KB '{name}' at {kb_path}: {e}")
            raise KnowledgeBaseError(f"Could not create directory for KB '{name}': {e}") from e

    async def list_kbs(self) -> Dict[str, str]:
        """Lists existing knowledge base directories and their descriptions asynchronously."""
        kbs: Dict[str, str] = {}
        # Base directory existence checked in __init__
        try:
            for d in self.base_dir.iterdir():
                if d.is_dir():
                    kb_name = d.name
                    config_path = d / "config.yaml"
                    description = "No description found." # Default description
                    if config_path.is_file():
                        try:
                            # Use asyncio.to_thread for synchronous file I/O
                            content = await asyncio.to_thread(config_path.read_text, encoding='utf-8')
                            data = yaml.safe_load(content)
                            if isinstance(data, dict) and "description" in data:
                                description = str(data["description"]) # Ensure it's a string
                        except (IOError, yaml.YAMLError, UnicodeDecodeError) as e:
                            logger.warning(f"Could not read/parse config for KB '{kb_name}': {e}")
                            description = f"Error reading description: {e}" # Provide error info
                        except Exception as e: # Catch unexpected errors
                            logger.error(f"Unexpected error reading config for KB '{kb_name}': {e}")
                            description = "Unexpected error reading description."
                    else:
                        logger.warning(f"Config file not found for KB '{kb_name}'. Using default description.")

                    kbs[kb_name] = description
            return kbs
        except OSError as e:
            logger.error(f"Error listing knowledge bases in {self.base_dir}: {e}")
            # Depending on requirements, could return {} or raise
            raise KnowledgeBaseError(f"Error listing knowledge bases: {e}") from e

    def delete_kb(self, name: str) -> None:
        """Deletes a knowledge base directory and its contents."""
        kb_path = self.get_kb_path(name)
        if not self.kb_exists(name):
            logger.warning(f"Attempted to delete non-existent knowledge base: {name}")
            raise KnowledgeBaseNotFoundError(f"Knowledge base '{name}' not found at {kb_path}")

        try:
            shutil.rmtree(kb_path)
            logger.info(f"Deleted knowledge base directory and contents: {kb_path}")
        except OSError as e:
            logger.error(f"Failed to delete knowledge base '{name}' at {kb_path}: {e}")
            raise KnowledgeBaseError(f"OS error deleting knowledge base '{name}': {e}") from e

    # --- Document Management (Requires RagManager Interaction) ---
    # Placeholder methods - Implementation requires RagManager
    def add_document(self, kb_name: str, doc_path: Path, doc_name: str | None = None):
        # 1. Check if kb exists (using self.kb_exists)
        # 2. Validate doc_path
        # 3. Determine final document name/ID
        # 4. Potentially copy/store the original doc inside kb_path/docs ?
        # 5. Call RagManager to process and index the document for this KB
        logger.info(f"Placeholder: Add document {doc_path} to KB {kb_name} (doc_name: {doc_name})")
        if not self.kb_exists(kb_name):
             raise KnowledgeBaseNotFoundError(f"Knowledge base '{kb_name}' not found.")
        # ... further implementation needed with RagManager ...

    def remove_document(self, kb_name: str, doc_name: str):
        # 1. Check if kb exists
        # 2. Call RagManager to remove the document and its index data for this KB
        # 3. Potentially remove original doc from kb_path/docs ?
        logger.info(f"Placeholder: Remove document {doc_name} from KB {kb_name}")
        if not self.kb_exists(kb_name):
             raise KnowledgeBaseNotFoundError(f"Knowledge base '{kb_name}' not found.")
        # ... further implementation needed with RagManager ...

    # Add query_kb etc. later, likely involving RagManager
    
    def migrate_all_configs(self) -> dict[str, bool]:
        """Migrates all knowledge base config files to lightrag-hku 1.4.0 format.
        
        Returns:
            Dictionary mapping KB names to migration status (True if migrated, False if not needed)
        """
        migration_results = {}
        
        if not self.base_dir.exists():
            logger.info("No knowledge base directory found, no migration needed")
            return migration_results
            
        logger.info(f"Scanning for knowledge bases to migrate in {self.base_dir}")
        
        for kb_path in self.base_dir.iterdir():
            if kb_path.is_dir():
                kb_name = kb_path.name
                logger.info(f"Checking KB '{kb_name}' for migration...")
                
                try:
                    migrated = migrate_config_file(kb_path)
                    migration_results[kb_name] = migrated
                    
                    if migrated:
                        logger.info(f"✅ Migrated KB '{kb_name}'")
                    else:
                        logger.info(f"ℹ️  KB '{kb_name}' - no migration needed")
                        
                except Exception as e:
                    logger.error(f"❌ Failed to migrate KB '{kb_name}': {e}")
                    migration_results[kb_name] = False
                    
        total_migrated = sum(1 for migrated in migration_results.values() if migrated)
        total_checked = len(migration_results)
        
        logger.info(f"Migration complete: {total_migrated}/{total_checked} knowledge bases migrated")
        return migration_results


def load_kb_query_config(kb_path: Path) -> dict[str, Any]:
    """Loads query configuration from config.yaml within a KB directory.

    Args:
        kb_path: The path to the knowledge base's root directory.

    Returns:
        A dictionary containing the query parameters, merged with defaults.
        Returns defaults if config.yaml is missing or invalid.
    """
    config_file_path = kb_path / "config.yaml"
    kb_name = kb_path.name
    kb_logger = logging.getLogger(f"kbmcp.{kb_name}")  # KB-specific logger for user_prompt messages
    
    # Attempt migration before loading config
    migrate_config_file(kb_path)
    
    loaded_config: dict[str, Any] = {}

    if config_file_path.is_file():
        logger.debug(f"Loading query config for KB '{kb_name}' from {config_file_path}")
        try:
            with open(config_file_path, 'r', encoding='utf-8') as f:
                loaded_data = yaml.safe_load(f)
                if isinstance(loaded_data, dict):
                    # Filter to only include keys relevant to query params
                    # Keep description if present, even if not in DEFAULT_QUERY_PARAMS strictly for querying
                    # Filter only query params, description is metadata now
                    loaded_config = {
                        k: v for k, v in loaded_data.items()
                        if k in DEFAULT_QUERY_PARAMS and k != "description"
                    }
                    logger.debug(f"Successfully loaded and filtered query config for KB '{kb_name}': {loaded_config}")
                    
                    # Log user_prompt specifically if present
                    if 'user_prompt' in loaded_config:
                        user_prompt_value = loaded_config['user_prompt']
                        if user_prompt_value and str(user_prompt_value).strip():
                            kb_logger.debug(f"Loaded user_prompt: '{user_prompt_value}'")
                        else:
                            kb_logger.debug("Empty user_prompt found in config")
                    else:
                        kb_logger.debug("No user_prompt configured, will use default empty value")
                elif loaded_data is None:
                    # Empty file, use defaults (excluding description)
                    logger.warning(f"Config file for KB '{kb_name}' is empty. Using default query parameters.")
                else:
                    # Invalid format (not a dictionary)
                    logger.error(f"Invalid config format in {config_file_path}. Expected a dictionary, got {type(loaded_data)}. Using default query parameters.")

        except yaml.YAMLError as e:
            logger.error(f"Error parsing YAML file {config_file_path}: {e}. Using default query parameters.")
        except OSError as e:
            logger.error(f"Error reading config file {config_file_path}: {e}. Using default query parameters.")
        except Exception as e:
            logger.error(f"Unexpected error loading config for KB '{kb_name}' from {config_file_path}: {e}. Using default query parameters.", exc_info=True)
    else:
        logger.debug(f"Config file not found for KB '{kb_name}' at {config_file_path}. Using default query parameters.")

    # Merge defaults with loaded (and filtered) config
    # Start with defaults, then overwrite with loaded values
    final_config = DEFAULT_QUERY_PARAMS.copy()
    final_config.update(loaded_config)
    
    # Validate user_prompt type (defensive programming)
    # user_prompt always exists due to DEFAULT_QUERY_PARAMS.copy() above
    if not isinstance(final_config['user_prompt'], str):
        logger.warning(f"Invalid user_prompt type in KB '{kb_name}' config: {type(final_config['user_prompt'])}. Converting to empty string.")
        final_config['user_prompt'] = ''

    # Log user_prompt in final config to confirm proper filtering and merging
    final_user_prompt = final_config.get('user_prompt', '')
    if final_user_prompt and str(final_user_prompt).strip():
        kb_logger.debug(f"Final config includes user_prompt: '{final_user_prompt}'")
    else:
        kb_logger.debug("Final config has empty user_prompt (using default)")

    logger.debug(f"Final query config for KB '{kb_name}': {final_config}")
    return final_config


def migrate_config_file(kb_path: Path) -> bool:
    """Migrates a config.yaml file from old parameter names to new ones.
    
    Args:
        kb_path: The path to the knowledge base's root directory.
        
    Returns:
        True if migration was performed, False if no migration was needed.
    """
    config_file_path = kb_path / "config.yaml"
    kb_name = kb_path.name
    kb_logger = logging.getLogger(f"kbmcp.{kb_name}")
    
    if not config_file_path.is_file():
        logger.debug(f"No config file to migrate for KB '{kb_name}'")
        return False
    
    # Parameter mapping from old names to new names
    PARAMETER_MIGRATION_MAP = {
        "max_token_for_text_unit": "max_entity_tokens",
        "max_token_for_global_context": "max_relation_tokens", 
        "max_token_for_local_context": "max_total_tokens"
    }
    
    try:
        # Read current config
        with open(config_file_path, 'r', encoding='utf-8') as f:
            config_data = yaml.safe_load(f)
            
        if not isinstance(config_data, dict):
            logger.debug(f"Config file for KB '{kb_name}' is not a dictionary, skipping migration")
            return False
            
        # Check if any old parameters exist
        old_params_found = []
        for old_param in PARAMETER_MIGRATION_MAP.keys():
            if old_param in config_data:
                old_params_found.append(old_param)
                
        if not old_params_found:
            logger.debug(f"No old parameters found in KB '{kb_name}' config, migration not needed")
            return False
            
        # Perform migration
        kb_logger.info("Migrating config file to lightrag-hku 1.4.0 format...")
        kb_logger.info(f"Found old parameters: {old_params_found}")
        
        migrated_config = config_data.copy()
        
        for old_param, new_param in PARAMETER_MIGRATION_MAP.items():
            if old_param in migrated_config:
                # Copy value to new parameter name
                migrated_config[new_param] = migrated_config[old_param]
                # Remove old parameter
                del migrated_config[old_param]
                kb_logger.info(f"Migrated '{old_param}' -> '{new_param}' (value: {migrated_config[new_param]})")
                
        # Create backup of original file
        backup_path = config_file_path.with_suffix('.yaml.backup')
        with open(backup_path, 'w', encoding='utf-8') as f:
            yaml.dump(config_data, f, default_flow_style=False)
        kb_logger.info(f"Created backup at {backup_path}")
        
        # Write migrated config
        with open(config_file_path, 'w', encoding='utf-8') as f:
            yaml.dump(migrated_config, f, default_flow_style=False)
            
        kb_logger.info(f"Successfully migrated config file for KB '{kb_name}'")
        return True
        
    except yaml.YAMLError as e:
        logger.error(f"Error parsing YAML file {config_file_path} during migration: {e}")
        return False
    except OSError as e:
        logger.error(f"Error reading/writing config file {config_file_path} during migration: {e}")
        return False
    except Exception as e:
        logger.error(f"Unexpected error during migration of {config_file_path}: {e}", exc_info=True)
        return False
