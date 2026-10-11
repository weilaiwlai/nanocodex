"""Minimal SDK test doubles, used only for offline adapter tests."""
import importlib.util
import importlib.machinery
import inspect
import sys
import types


def install_if_missing():
    if 'agents' in sys.modules or importlib.util.find_spec('agents') is not None:
        return
    def module(name):
        value = types.ModuleType(name)
        value.__spec__ = importlib.machinery.ModuleSpec(name, loader=None)
        value.__path__ = []
        sys.modules[name] = value
        return value
    agents = module('agents')
    class Wrapper:
        def __class_getitem__(cls, item): return cls
    class Agent:
        def __init__(self, **kwargs): self.__dict__.update(kwargs)
    class Config:
        def __init__(self, **kwargs): self.__dict__.update(kwargs)
    class Tool:
        def __init__(self, function, **options):
            self.name = options.get('name_override', function.__name__)
            self.description = options.get('description_override', '')
            self.params_json_schema = {'properties': {p: {'type': 'string'} for p in inspect.signature(function).parameters if p != 'ctx'}}
            self.function = function
    agents.RunContextWrapper, agents.Agent, agents.RunConfig = Wrapper, Agent, Config
    agents.Runner = types.SimpleNamespace(run=None, run_streamed=None)
    agents.TResponseInputItem = dict
    agents.ModelSettings = Config
    agents.function_tool = lambda function, **options: Tool(function, **options)
    for name in ('set_default_openai_api', 'set_default_openai_client', 'set_tracing_disabled'):
        setattr(agents, name, lambda *args, **kwargs: None)
    items = module('agents.items')
    items.ToolCallItem = type('ToolCallItem', (), {})
    items.ToolCallOutputItem = type('ToolCallOutputItem', (), {})
    configuration = module('agents.run_config')
    configuration.ModelInputData = Config
    openai = module('openai')
    openai.AsyncOpenAI = Config
    openai.APIConnectionError = type('APIConnectionError', (Exception,), {})
    module('openai.types')
    responses = module('openai.types.responses')
    responses.ResponseTextDeltaEvent = Config
    tokenizer = module('tiktoken')
    class Encoding:
        def encode(self, text, **kwargs): return list(range((len(text.encode('utf-8')) + 2) // 3))
    tokenizer.encoding_for_model = lambda model: Encoding()
    tokenizer.get_encoding = lambda name: Encoding()
