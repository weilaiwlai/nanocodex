"""Context helpers loaded on demand; storage and budget checks also work offline."""
from importlib import import_module

_BUILDER = {
    "StableContextLayer", "RepoRuleLayer", "LongTermMemoryLayer", "RuntimeContextLayer",
    "ContextBundle", "build_stable_context_layer", "build_repo_rule_layer",
    "build_long_term_memory_layer", "build_context_bundle",
}
_MENTIONS = {"FileMentionPreprocessResult", "extract_file_mentions", "build_file_mention_reminder", "preprocess_user_input"}
__all__ = sorted(_BUILDER | _MENTIONS)


def __getattr__(name):
    if name in _BUILDER | _MENTIONS:
        module = "context_builder" if name in _BUILDER else "file_mentions"
        value = getattr(import_module(f"src.context.{module}"), name)
        globals()[name] = value
        return value
    raise AttributeError(name)
