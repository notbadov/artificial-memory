"""Direct pytest invocations share the canonical runner's explicit isolation context."""

from tools.test_isolation import isolated_client_state


def pytest_configure(config):
    context = isolated_client_state()
    context.__enter__()
    config._agent_platform_client_state = context


def pytest_unconfigure(config):
    context = getattr(config, "_agent_platform_client_state", None)
    if context is not None:
        del config._agent_platform_client_state
        context.__exit__(None, None, None)
