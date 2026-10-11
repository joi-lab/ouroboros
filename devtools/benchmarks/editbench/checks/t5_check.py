"""Behavior check for t5_overhaul (run with PYTHONPATH=<candidate workspace>)."""
import provider_models as pm

assert pm.provider_for_model("openai::gpt-5.6") == "openai"
assert pm.provider_for_model("some/openrouter-model") == "openrouter"
assert pm.provider_for_model("x (local)") == "local"
assert "OPENROUTER_API_KEY" in pm.MODEL_PROVIDER_CREDENTIAL_KEYS
assert pm.PROVIDER_CREDENTIAL_GROUPS["local"] == ()
print("OK")
