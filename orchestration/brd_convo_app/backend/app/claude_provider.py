import logging
import os
import ssl
import time
from typing import Optional

try:
    from anthropic import Anthropic
except Exception:
    Anthropic = None

# Read at import time for convenience, but _get_client() re-reads at call time
# so the test mock env-var override (set before first call) is respected.
CLAUDE_MODEL_DEFAULT = "claude-sonnet-4-5"

_logger = logging.getLogger(__name__)


def _build_httpx_client():
    """
    Return an httpx.Client configured for the current network environment.

    Corporate networks that perform SSL inspection replace server certificates
    with an internal CA certificate.  Python's bundled CA store does not trust
    that CA, so every HTTPS call raises SSLCertVerificationError ("self-signed
    certificate in certificate chain"), which surfaces to the user as a
    frustrating "Connection error."

    Resolution order:
      1. CLAUDE_CA_BUNDLE env var → path to a PEM file that contains the
         corporate root CA.  Recommended: export your organisation's root CA
         from the Windows certificate store (certmgr.msc → Trusted Root
         Certification Authorities → export as Base-64 PEM) and set this
         variable to that file path.
      2. REQUESTS_CA_BUNDLE / CURL_CA_BUNDLE → standard env vars used by many
         tools; picked up automatically so the user only needs to set one.
      3. SSL_CERT_FILE → another common override.
      4. CLAUDE_SSL_VERIFY=false → disables SSL verification entirely.
         Use only as a last resort on a trusted private network.
      5. Default → let httpx use its bundled certifi CA store (normal prod
         behaviour on networks without SSL inspection).
    """
    try:
        import httpx
    except ImportError:
        return None  # httpx not installed; Anthropic SDK will use its own default

    # ── Priority 1-3: custom CA bundle ────────────────────────────────────────
    ca_bundle = (
        os.getenv("CLAUDE_CA_BUNDLE")
        or os.getenv("REQUESTS_CA_BUNDLE")
        or os.getenv("CURL_CA_BUNDLE")
        or os.getenv("SSL_CERT_FILE")
    )
    if ca_bundle and os.path.isfile(ca_bundle):
        _logger.info("claude_provider: using custom CA bundle: %s", ca_bundle)
        return httpx.Client(verify=ca_bundle)

    # ── Priority 4: explicit SSL-verify override ───────────────────────────────
    ssl_verify_env = os.getenv("CLAUDE_SSL_VERIFY", "true").strip().lower()
    if ssl_verify_env in ("false", "0", "no"):
        _logger.warning(
            "claude_provider: SSL certificate verification DISABLED "
            "(CLAUDE_SSL_VERIFY=false). Set CLAUDE_CA_BUNDLE to your corporate "
            "root CA for a secure solution."
        )
        return httpx.Client(verify=False)

    # ── Priority 5: try Windows certificate store via truststore ──────────────
    try:
        import truststore
        ctx = truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        _logger.info("claude_provider: using Windows/system certificate store (truststore)")
        return httpx.Client(verify=ctx)
    except ImportError:
        pass  # truststore not installed — fall through to default
    except Exception as _ts_err:
        _logger.debug("claude_provider: truststore unavailable: %s", _ts_err)

    # ── Default: certifi bundle (works on most networks without inspection) ────
    return None  # None → Anthropic SDK picks its own default httpx.Client


def _get_client():
    """Return an Anthropic client, or None when CLAUDE_MOCK=1."""
    mock_mode = os.getenv("CLAUDE_MOCK", "0") == "1"
    if mock_mode:
        return None
    if Anthropic is None:
        raise RuntimeError("Anthropic SDK is not installed. Run: pip install anthropic")
    api_key = os.getenv("CLAUDE_API_KEY")
    if not api_key:
        raise RuntimeError(
            "CLAUDE_API_KEY is not set. Add it to your .env file or environment."
        )
    http_client = _build_httpx_client()
    if http_client is not None:
        return Anthropic(api_key=api_key, http_client=http_client)
    return Anthropic(api_key=api_key)


# ─────────────────────────────────────────────────────────────────────────────
# Azure OpenAI provider (Azure AD / `az login` auth — no API key)
# ─────────────────────────────────────────────────────────────────────────────
# Reaches the deployment with an Azure AD bearer token from DefaultAzureCredential
# (i.e. the machine is logged in via the Azure CLI), exactly like bristlecone_llm.py.
# Selected when LLM_PROVIDER=azure (the default). All values are overridable via env.
_AZURE_CLIENT = None
AZURE_OPENAI_ENDPOINT_DEFAULT    = "https://ai-adoption-coe.services.ai.azure.com"
AZURE_OPENAI_DEPLOYMENT_DEFAULT  = "gpt-5.4"
AZURE_OPENAI_API_VERSION_DEFAULT = "2025-01-01-preview"


def _get_azure_client():
    """Lazily build and cache an AzureOpenAI client authenticated with Azure AD
    (DefaultAzureCredential → works with `az login`). The azure/openai packages
    are imported lazily so they're only required when the Azure provider is used."""
    global _AZURE_CLIENT
    if _AZURE_CLIENT is not None:
        return _AZURE_CLIENT
    from azure.identity import DefaultAzureCredential, get_bearer_token_provider
    from openai import AzureOpenAI
    endpoint    = os.getenv("AZURE_OPENAI_ENDPOINT", AZURE_OPENAI_ENDPOINT_DEFAULT)
    api_version = os.getenv("AZURE_OPENAI_API_VERSION", AZURE_OPENAI_API_VERSION_DEFAULT)
    token_provider = get_bearer_token_provider(
        DefaultAzureCredential(),
        "https://cognitiveservices.azure.com/.default",
    )
    # Fail fast on a stalled call (default 90s) instead of hanging ~600s, and
    # disable the SDK's own retry loop so all retry logic lives in one place.
    try:
        _timeout = float(os.getenv("AZURE_OPENAI_TIMEOUT", "90"))
    except ValueError:
        _timeout = 90.0
    # Corporate proxy with SSL inspection: set AZURE_SSL_VERIFY=false in .env
    # to skip certificate verification (mirrors CLAUDE_SSL_VERIFY behaviour).
    _extra = {}
    if os.getenv("AZURE_SSL_VERIFY", "true").strip().lower() in ("false", "0", "no"):
        import httpx
        _extra["http_client"] = httpx.Client(verify=False)
        _logger.warning("claude_provider(azure): SSL verification DISABLED (AZURE_SSL_VERIFY=false).")
    _AZURE_CLIENT = AzureOpenAI(
        azure_endpoint=endpoint,
        azure_ad_token_provider=token_provider,
        api_version=api_version,
        timeout=_timeout,
        max_retries=0,
        **_extra,
    )
    _logger.info(
        "claude_provider: using Azure OpenAI deployment %s at %s (timeout=%ss, max_retries=0)",
        os.getenv("AZURE_OPENAI_DEPLOYMENT", AZURE_OPENAI_DEPLOYMENT_DEFAULT), endpoint, _timeout,
    )
    return _AZURE_CLIENT


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default


def _retry_after_seconds(exc: Exception, max_wait: float) -> float:
    """Honour Azure's Retry-After header (seconds) when throttled (429)."""
    try:
        resp = getattr(exc, "response", None)
        if resp is not None and getattr(resp, "headers", None):
            ra = resp.headers.get("retry-after") or resp.headers.get("Retry-After")
            if ra:
                return min(float(ra), max_wait)
    except Exception:
        pass
    return min(10.0, max_wait)


def _reduced_prompt(prompt: str) -> str:
    """Fallback prompt when the content filter blocks the full one (usually the
    protected-material category tripping on verbatim source text)."""
    head = ("Summarise the following in your own words to answer the request. "
            "Do NOT quote or reproduce the source text verbatim.\n\n")
    return head + (prompt or "")[:8000]


def _azure_completion(prompt: str, max_tokens: int, max_retries: int,
                      reasoning_effort: Optional[str] = None,
                      max_output_cap: Optional[int] = None,
                      system_prompt: Optional[str] = None) -> str:
    """Single-turn completion via Azure OpenAI.

    gpt-5.x deployments use ``max_completion_tokens`` (not ``max_tokens``), only
    accept the default temperature, and share the token budget between hidden
    reasoning and the visible answer. We:
      • set reasoning_effort (default "none" via AZURE_OPENAI_REASONING_EFFORT) so
        the model doesn't burn the whole budget "thinking" and return empty;
      • cap output tokens (AZURE_OPENAI_MAX_OUTPUT_TOKENS, default 2000) to avoid
        TPM throttling;
      • run a single generation attempt (AZURE_OPENAI_GEN_ATTEMPTS, default 1);
      • handle 429 with Retry-After on a SEPARATE budget so throttling doesn't
        consume the generation attempts;
      • fail fast on content_filter, with one reduced-prompt fallback.
    """
    client = _get_azure_client()
    deployment = os.getenv("AZURE_OPENAI_DEPLOYMENT", AZURE_OPENAI_DEPLOYMENT_DEFAULT)

    # A caller can pass max_output_cap to opt out of the global generation cap
    # (e.g. user-story extraction needs a larger JSON array than a section body).
    cap = max_output_cap if max_output_cap else _env_int(
        "AZURE_OPENAI_MAX_OUTPUT_TOKENS", _env_int("BDG_MAX_OUTPUT_TOKENS", 2000))
    effective_tokens = min(max(max_tokens, 1), max(cap, 1))

    # reasoning_effort: explicit arg wins, else env default "none". "none" is a
    # valid value for gpt-5.5 ("minimal" is rejected). Empty/off → don't send it.
    effort = (reasoning_effort or os.getenv("AZURE_OPENAI_REASONING_EFFORT", "none") or "").strip().lower()
    use_effort = effort not in ("", "off", "default", "auto")

    gen_attempts = max(1, _env_int("AZURE_OPENAI_GEN_ATTEMPTS", 1))
    rl_retries   = max(0, _env_int("AZURE_OPENAI_RATE_LIMIT_RETRIES", 6))
    rl_max_wait  = _env_float("AZURE_OPENAI_RATE_LIMIT_MAX_WAIT", 70.0)

    # ── Truncation safety net ──────────────────────────────────────────────
    # A non-empty response with finish_reason="length" is a *cut-off* answer,
    # not a failed one — the loop below only treats *empty* content as
    # retryable, so a truncated-but-non-empty answer would otherwise be
    # returned to the caller looking exactly like a complete one. Allow
    # exactly one budget-doubling retry when that happens, capped well below
    # what the deployment is likely to reject outright.
    truncation_retried = False
    truncation_ceiling = min(max(cap, effective_tokens) * 2, 32000)
    best_text: Optional[str] = None
    best_finish: Optional[str] = None

    def _create(send_effort: bool, p: str, toks: int):
        msgs = []
        if system_prompt:
            msgs.append({"role": "system", "content": system_prompt})
        msgs.append({"role": "user", "content": p})
        kw = dict(model=deployment, messages=msgs, max_completion_tokens=toks)
        if send_effort:
            kw["reasoning_effort"] = effort
        return client.chat.completions.create(**kw)

    cur_prompt = prompt
    reduced_tried = False
    rl_used = 0
    gen = 0
    while gen < gen_attempts:
        gen += 1
        try:
            try:
                response = _create(use_effort, cur_prompt, effective_tokens)
            except Exception as eff_exc:
                m = str(eff_exc).lower()
                if use_effort and ("reasoning_effort" in m or "unsupported" in m
                                   or "unexpected" in m or "unknown" in m
                                   or "not supported" in m):
                    _logger.warning("claude_provider(azure): reasoning_effort=%r not accepted (%s) — retrying without it.", effort, eff_exc)
                    use_effort = False
                    response = _create(False, cur_prompt, effective_tokens)
                else:
                    raise
            choices = getattr(response, "choices", None) or []
            ch0 = choices[0] if choices else None
            finish = getattr(ch0, "finish_reason", "unknown") if ch0 else "no-choices"
            text = (ch0.message.content or "").strip() if ch0 else ""
            _logger.info(
                "claude_provider(azure): gen=%d/%d deployment=%s finish=%s chars=%d budget=%d effort=%s",
                gen, gen_attempts, deployment, finish, len(text), effective_tokens,
                effort if use_effort else "off",
            )
            if text:
                if finish == "length" and not truncation_retried and effective_tokens < truncation_ceiling:
                    # Non-empty but cut off mid-output — silently returning this
                    # looks identical to a complete answer to the caller. Retry
                    # once with a bigger budget before accepting it; if the
                    # retry is *also* truncated, return whichever attempt is
                    # longer rather than lose content, and log loudly so the
                    # truncation is at least visible in the logs.
                    truncation_retried = True
                    best_text, best_finish = text, finish
                    effective_tokens = min(effective_tokens * 2, truncation_ceiling)
                    _logger.warning(
                        "claude_provider(azure): response truncated (finish_reason=length, "
                        "%d chars) — retrying once with budget=%d.",
                        len(text), effective_tokens,
                    )
                    gen -= 1  # don't spend a real generation attempt on this retry
                    continue
                if finish == "length" and best_text is not None and len(best_text) >= len(text):
                    # The retry didn't improve things — keep the longer of the two.
                    _logger.warning(
                        "claude_provider(azure): retry still truncated (finish_reason=length) "
                        "and not longer than the first attempt — returning the longer draft "
                        "(%d chars). Output may be incomplete.", len(best_text),
                    )
                    return best_text
                if finish == "length":
                    _logger.warning(
                        "claude_provider(azure): returning truncated output (finish_reason=length, "
                        "%d chars) — output may be incomplete even after retry.", len(text),
                    )
                return text
            if finish == "content_filter":
                if not reduced_tried:
                    reduced_tried = True
                    cur_prompt = _reduced_prompt(prompt)
                    gen -= 1  # don't spend a gen attempt on the reduced retry
                    _logger.warning("claude_provider(azure): content_filter — retrying once with reduced/summarise prompt.")
                    continue
                raise RuntimeError(f"Azure content filter blocked the request (deployment={deployment})")
            # Empty (usually finish_reason=length): bump budget modestly and retry.
            effective_tokens = min(effective_tokens * 2, max(cap, 8000))
        except Exception as exc:
            m = str(exc).lower()
            status = getattr(exc, "status_code", None)
            is_429 = status == 429 or "429" in m or "too many requests" in m or "rate limit" in m
            is_filter = ("content_filter" in m or "content management policy" in m
                         or "responsible ai" in m or "jailbreak" in m)
            if is_filter:
                if not reduced_tried:
                    reduced_tried = True
                    cur_prompt = _reduced_prompt(prompt)
                    gen -= 1
                    _logger.warning("claude_provider(azure): content_filter error — retrying once with reduced prompt.")
                    continue
                raise
            if is_429 and rl_used < rl_retries:
                rl_used += 1
                wait = _retry_after_seconds(exc, rl_max_wait)
                _logger.warning("claude_provider(azure): 429 throttled — waiting %.1fs (rate-limit retry %d/%d).", wait, rl_used, rl_retries)
                time.sleep(wait)
                gen -= 1  # 429 doesn't consume a generation attempt
                continue
            transient = ("timeout" in m or "overloaded" in m or "temporarily" in m or "connection" in m)
            if transient and gen < gen_attempts:
                _logger.warning("claude_provider(azure): transient error (%s) — retrying.", exc)
                time.sleep(2)
                continue
            _logger.warning("claude_provider(azure): API error: %s", exc)
            raise
    raise RuntimeError(
        f"Azure OpenAI returned empty content after {gen_attempts} attempt(s) (deployment={deployment})"
    )


def completion_from_prompt(
    prompt: str,
    model: Optional[str] = None,
    max_tokens: int = 800,
    temperature: float = 0.0,
    max_retries: int = 3,
    reasoning_effort: Optional[str] = None,
    max_output_cap: Optional[int] = None,
    system_prompt: Optional[str] = None,
) -> str:
    """
    Send a single-turn prompt to Anthropic Claude and return the assistant text.

    Uses the modern ``client.messages.create()`` API (Anthropic SDK >= 0.18).
    Falls back to a deterministic mock response when CLAUDE_MOCK=1 so tests
    can run without credentials.

    ``system_prompt``, when given, is sent as a system message (Azure/OpenAI
    path) or the top-level ``system`` field (Anthropic path). Every existing
    caller omits it, so behaviour is unchanged unless a caller opts in.

    Retry policy (empty content or transient errors):
      Attempt 1 → immediate
      Attempt 2 → 2 s wait
      Attempt 3 → 4 s wait
    Rate-limit / overloaded responses get a longer 5× backoff.
    Raises RuntimeError after all retries are exhausted with empty content.
    """
    # Mock mode short-circuit (provider-agnostic) so tests run without creds.
    if os.getenv("CLAUDE_MOCK", "0") == "1":
        summary = " ".join(prompt.split())
        return f"[MOCK CLAUDE RESPONSE] {summary[:400]}"

    # ── Provider selection ───────────────────────────────────────────────────
    # Default to Azure OpenAI (Azure AD / `az login`). Set LLM_PROVIDER=anthropic
    # to use the Anthropic API instead. The `model` argument is only meaningful
    # for the Anthropic path; the Azure path uses AZURE_OPENAI_DEPLOYMENT.
    provider = os.getenv("LLM_PROVIDER", "azure").strip().lower()
    if provider in ("azure", "azure_openai", "azureopenai", "aoai"):
        return _azure_completion(prompt, max_tokens, max_retries,
                                 reasoning_effort=reasoning_effort,
                                 max_output_cap=max_output_cap,
                                 system_prompt=system_prompt)

    resolved_model = model or os.getenv("CLAUDE_MODEL", CLAUDE_MODEL_DEFAULT)
    client = _get_client()

    # Mock mode: return a deterministic stub so tests can run without credentials.
    if client is None:
        summary = " ".join(prompt.split())
        return f"[MOCK CLAUDE RESPONSE] {summary[:400]}"

    last_exc: Optional[Exception] = None
    stop_reason: str = "unknown"

    # ── Truncation safety net (mirrors the Azure path) ──────────────────────
    # stop_reason="max_tokens" with non-empty text is a cut-off answer, not a
    # failed one — returning it as-is looks identical to a complete answer to
    # the caller. Allow one budget-doubling retry, capped well under what the
    # model is likely to reject outright.
    truncation_retried = False
    truncation_ceiling = min(max_tokens * 2, 32000)
    cur_max_tokens = max_tokens
    best_text: Optional[str] = None

    for attempt in range(1, max_retries + 1):
        try:
            create_kwargs = dict(
                model=resolved_model,
                max_tokens=cur_max_tokens,
                temperature=temperature,
                messages=[{"role": "user", "content": prompt}],
            )
            if system_prompt:
                create_kwargs["system"] = system_prompt
            response = client.messages.create(**create_kwargs)

            stop_reason = getattr(response, "stop_reason", "unknown")
            content_blocks = response.content  # list[ContentBlock]
            n_blocks = len(content_blocks) if content_blocks else 0

            _logger.info(
                "claude_provider: attempt=%d/%d model=%s stop_reason=%s content_blocks=%d budget=%d",
                attempt, max_retries, resolved_model, stop_reason, n_blocks, cur_max_tokens,
            )

            # ── Extract text ────────────────────────────────────────────────
            if content_blocks and hasattr(content_blocks[0], "text"):
                text = content_blocks[0].text.strip()
                if text:
                    if (stop_reason == "max_tokens" and not truncation_retried
                            and cur_max_tokens < truncation_ceiling and attempt < max_retries):
                        truncation_retried = True
                        best_text = text
                        cur_max_tokens = min(cur_max_tokens * 2, truncation_ceiling)
                        _logger.warning(
                            "claude_provider: response truncated (stop_reason=max_tokens, "
                            "%d chars) — retrying once with budget=%d.",
                            len(text), cur_max_tokens,
                        )
                        continue
                    if stop_reason == "max_tokens" and best_text is not None and len(best_text) >= len(text):
                        _logger.warning(
                            "claude_provider: retry still truncated and not longer than the "
                            "first attempt — returning the longer draft (%d chars). Output "
                            "may be incomplete.", len(best_text),
                        )
                        return best_text
                    if stop_reason == "max_tokens":
                        _logger.warning(
                            "claude_provider: returning truncated output (stop_reason=max_tokens, "
                            "%d chars) — output may be incomplete even after retry.", len(text),
                        )
                    return text
                # Empty text block — treat as a retryable condition.
                _logger.warning(
                    "claude_provider: empty text on attempt %d/%d "
                    "(stop_reason=%s, model=%s) — will retry.",
                    attempt, max_retries, stop_reason, resolved_model,
                )
            else:
                _logger.warning(
                    "claude_provider: unexpected content structure on attempt %d/%d: "
                    "blocks=%r stop_reason=%s — will retry.",
                    attempt, max_retries, content_blocks, stop_reason,
                )

        except Exception as exc:
            last_exc = exc
            err_str = str(exc)
            is_rate_limit = (
                "429" in err_str
                or "rate_limit" in err_str.lower()
                or "overloaded" in err_str.lower()
                or "too many requests" in err_str.lower()
            )
            is_network = (
                "connection" in err_str.lower()
                or "timeout" in err_str.lower()
                or "socket" in err_str.lower()
            )
            _logger.warning(
                "claude_provider: API error on attempt %d/%d: %s",
                attempt, max_retries, err_str,
            )
            # Only retry on transient/rate-limit errors; re-raise everything else
            # immediately so auth errors, bad requests, etc. surface right away.
            if not (is_rate_limit or is_network):
                raise
            if attempt < max_retries:
                wait = (2 ** attempt) * (5 if is_rate_limit else 1)
                _logger.info("claude_provider: waiting %ds before retry.", wait)
                time.sleep(wait)
            else:
                raise

        # Wait before the next attempt (empty-content path)
        if attempt < max_retries:
            wait = 2 ** attempt  # 2s, 4s
            _logger.info("claude_provider: waiting %ds before next attempt.", wait)
            time.sleep(wait)

    # All attempts returned empty content (no exception was raised from the loop).
    raise RuntimeError(
        f"Claude returned empty content after {max_retries} attempts "
        f"(model={resolved_model}, last_stop_reason={stop_reason})"
    )

