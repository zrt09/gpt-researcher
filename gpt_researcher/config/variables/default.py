from .base import BaseConfig

DEFAULT_CONFIG: BaseConfig = {
    "RETRIEVER": "tavily",
    "EMBEDDING": "openai:text-embedding-3-small",
    "SIMILARITY_THRESHOLD": 0.42,
    "CONTEXT_FILTER": "auto",  # auto | jev | keyword | embeddings | none; auto = jev with TYPESAFE_API_KEY, else keyword
    "FAST_LLM": "openai:gpt-5.4-mini",
    "SMART_LLM": "openai:gpt-6-sol",  # Has support for long responses (2k+ words).
    "STRATEGIC_LLM": "openai:gpt-5.4",  # Reasoning model used for planning; tune REASONING_EFFORT for speed vs. depth.
    # Output token limits. For reasoning models (the default gpt-5.x family)
    # these map to max_completion_tokens, which also covers reasoning tokens -
    # hence the generous headroom on top of the visible output.
    "FAST_TOKEN_LIMIT": 6000,
    "SMART_TOKEN_LIMIT": 12000,
    "STRATEGIC_TOKEN_LIMIT": 8000,
    "BROWSE_CHUNK_MAX_LENGTH": 8192,
    "CURATE_SOURCES": False,
    "SUMMARY_TOKEN_LIMIT": 700,
    "TEMPERATURE": 0.4,
    "USER_AGENT": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/119.0.0.0 Safari/537.36 Edg/119.0.0.0",
    "MAX_SEARCH_RESULTS_PER_QUERY": 5,
    "MEMORY_BACKEND": "local",
    "TOTAL_WORDS": 1200,
    "REPORT_FORMAT": "APA",
    "MAX_ITERATIONS": 3,
    "AGENT_ROLE": None,
    "SCRAPER": "bs",
    "MAX_SCRAPER_WORKERS": 15,
    "SCRAPER_RATE_LIMIT_DELAY": 0.0,  # Minimum seconds between scraper requests (0 = no limit, useful for API rate limiting)
    "MAX_SUBTOPICS": 3,
    "LANGUAGE": "english",
    "REPORT_SOURCE": "web",
    "DOC_PATH": "./my-docs",
    "PROMPT_FAMILY": "default",
    "LLM_KWARGS": {},
    "EMBEDDING_KWARGS": {},
    "VERBOSE": False,
    # Deep research specific settings
    "DEEP_RESEARCH_BREADTH": 3,
    "DEEP_RESEARCH_DEPTH": 2,
    "DEEP_RESEARCH_CONCURRENCY": 4,
    
    # MCP retriever specific settings
    "MCP_SERVERS": [],  # List of predefined MCP server configurations
    "MCP_AUTO_TOOL_SELECTION": True,  # Whether to automatically select the best tool for a query
    "MCP_ALLOWED_ROOT_PATHS": [],  # List of allowed root paths for local file access
    "MCP_STRATEGY": "fast",  # MCP execution strategy: "fast", "deep", "disabled"
    "REASONING_EFFORT": "medium",
    
    # Image generation settings (optional - requires GOOGLE_API_KEY)
    # Free tier models: gemini-2.5-flash-image
    # Paid tier models: imagen-4.0-generate-001, imagen-4.0-fast-generate-001
    "IMAGE_GENERATION_MODEL": "models/gemini-2.5-flash-image",
    "IMAGE_GENERATION_MAX_IMAGES": 3,  # Maximum number of images to generate per report
    "IMAGE_GENERATION_ENABLED": False,  # Master switch for inline image generation
    "IMAGE_GENERATION_STYLE": "dark",  # Image style: "dark" (matches app theme), "light", or "auto"
    "IMAGE_GENERATION_PROVIDER": "google",  # Image provider: "google" or "modelslab"

    # Adaptive research settings (see docs/改造方案/05_改造后总体架构设计.md)
    # Disabled by default: the original one-shot pipeline is unchanged unless
    # ADAPTIVE_ENABLED=true is set explicitly.
    "ADAPTIVE_ENABLED": False,
    "ADAPTIVE_MAX_ROUNDS": 3,
    "ADAPTIVE_MAX_LLM_CALLS": 120,
    "ADAPTIVE_MAX_TOKENS": 300000,
    "ADAPTIVE_MAX_SECONDS": 900,
    "ADAPTIVE_MAX_COST_USD": 0.15,
    "ADAPTIVE_MAX_TASKS": 6,
    "ADAPTIVE_PLAN_TASKS": 5,  # initial plan size; gap tasks may add up to MAX_TASKS
    "ADAPTIVE_MAX_TASKS_PER_ROUND": 4,
    "ADAPTIVE_CONCURRENCY": 3,
    "ADAPTIVE_EVAL_MODE": "light",  # light = zero-cost metrics only; full = adds LLM judging
    "ADAPTIVE_MIN_COVERAGE": 0.70,
    "ADAPTIVE_MIN_SUPPORT_RATE": 0.85,
    "ADAPTIVE_MIN_CITATION_MATCH": 0.80,
    "ADAPTIVE_MIN_COMPLETENESS": 0.90,
    "ADAPTIVE_MAX_CONFLICT": 0.10,
    "ADAPTIVE_NO_PROGRESS_ROUNDS": 2,
    "ADAPTIVE_VALIDITY_SAMPLE": 20,
    "ADAPTIVE_FRESHNESS_MONTHS": 24,
    "ADAPTIVE_STATE_DIR": "outputs/adaptive",
}
