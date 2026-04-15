"""
LiveKit voice agent with OpenTelemetry tracing for Coval simulation testing.

Architecture: LiveKit Agents SDK with SIP dispatch. Each call is a LiveKit room
joined by the agent via a dispatch rule keyed on agent_name="livekit-voice-agent-otel".

Tracing: DynamicCovalExporter buffers spans until the Coval simulation ID is known.
         The simulation ID arrives as the SIP header X-Coval-Simulation-Id,
         surfaced via participant.attributes when a SIP caller joins the room.
         Fallback: COVAL_SIMULATION_ID env var (set for local testing).

Span schema (SIM-328 + SIM-329 attributes):
  stt          stt.transcription, metrics.ttfb, stt.confidence
    └── stt.provider.deepgram    stt.providerName, stt.confidence, metrics.ttfb
  llm          metrics.ttfb, llm.finish_reason, gen_ai.usage.input_tokens,
               gen_ai.usage.output_tokens
  tts          metrics.ttfb

Notes on confidence and finish_reason:
  stt.confidence  — synthetic 0.95. LiveKit's metrics API does not expose per-
                    utterance ASR confidence. Real confidence is available if you
                    hook directly into the Deepgram websocket response.
  llm.finish_reason — derived by observing function_tools_executed before the
                      next LLMMetrics event. The pending span approach buffers
                      each LLM span until we know whether tools were called.
"""

import json
import os
import threading
import time
from typing import Optional, Sequence

import requests
from dotenv import load_dotenv
from livekit import agents, rtc
from livekit.agents import AgentServer, AgentSession, Agent, function_tool, room_io
from livekit.agents import metrics as agent_metrics
from livekit.agents.voice.events import (
    FunctionToolsExecutedEvent,
    MetricsCollectedEvent,
    UserInputTranscribedEvent,
)
from livekit.plugins import (
    deepgram,
    noise_cancellation,
    openai as livekit_openai,
    silero,
)
from livekit.plugins.turn_detector.multilingual import MultilingualModel
from opentelemetry import trace as otel_trace
from opentelemetry.sdk.resources import SERVICE_NAME, Resource
from opentelemetry.sdk.trace import ReadableSpan, TracerProvider
from opentelemetry.sdk.trace.export import (
    SimpleSpanProcessor,
    SpanExporter,
    SpanExportResult,
)

load_dotenv(".env.local")

COVAL_TRACES_ENDPOINT = "https://api.coval.dev/v1/traces"
COVAL_API_KEYS_JSON = os.environ.get("COVAL_API_KEYS_JSON", "")
COVAL_API_KEYS_FILE = os.environ.get("COVAL_API_KEYS_FILE", "")
COVAL_API_KEYS_REFRESH_SECONDS = max(
    float(os.environ.get("COVAL_API_KEYS_REFRESH_SECONDS", "30")), 0.0
)


# ── Tracing ────────────────────────────────────────────────────────────────────


def _span_to_otlp_json(span: ReadableSpan) -> dict:
    """Convert a ReadableSpan to OTLP JSON format (resourceSpans structure)."""

    def attrs(attributes) -> list:
        if not attributes:
            return []
        result = []
        for k, v in attributes.items():
            if isinstance(v, bool):
                result.append({"key": k, "value": {"boolValue": v}})
            elif isinstance(v, int):
                result.append({"key": k, "value": {"intValue": v}})
            elif isinstance(v, float):
                result.append({"key": k, "value": {"doubleValue": v}})
            else:
                result.append({"key": k, "value": {"stringValue": str(v)}})
        return result

    def hex_id(id_int: int, length: int) -> str:
        return format(id_int, f"0{length * 2}x") if id_int else ""

    context = span.context
    span_dict = {
        "traceId": hex_id(context.trace_id, 16) if context else "",
        "spanId": hex_id(context.span_id, 8) if context else "",
        "parentSpanId": hex_id(span.parent.span_id, 8) if span.parent else "",
        "name": span.name,
        "kind": span.kind.value,
        "startTimeUnixNano": str(span.start_time) if span.start_time else "0",
        "endTimeUnixNano": str(span.end_time) if span.end_time else "0",
        "attributes": attrs(span.attributes),
        "status": {
            "code": span.status.status_code.value,
            "message": span.status.description or "",
        },
        "events": [],
        "links": [],
    }

    resource_attrs = attrs(span.resource.attributes) if span.resource else []
    return {
        "resourceSpans": [
            {
                "resource": {"attributes": resource_attrs},
                "scopeSpans": [
                    {
                        "scope": {
                            "name": span.instrumentation_scope.name
                            if span.instrumentation_scope
                            else ""
                        },
                        "spans": [span_dict],
                    }
                ],
            }
        ]
    }


class _ApiKeyStore:
    """Loads Coval trace API keys from file, JSON, or env vars."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._cached_items: list[tuple[str, str]] = []
        self._last_checked_at = 0.0
        self._file_mtime: Optional[float] = None

    def get_items(self) -> list[tuple[str, str]]:
        with self._lock:
            if COVAL_API_KEYS_FILE:
                self._refresh_from_file_if_needed()
                return list(self._cached_items)

            if not self._cached_items:
                self._cached_items = self._load_static_items()
            return list(self._cached_items)

    def _refresh_from_file_if_needed(self) -> None:
        now = time.time()
        if (
            self._cached_items
            and now - self._last_checked_at < COVAL_API_KEYS_REFRESH_SECONDS
        ):
            return

        self._last_checked_at = now
        try:
            stat = os.stat(COVAL_API_KEYS_FILE)
        except OSError as exc:
            if not self._cached_items:
                print(
                    f"[coval] unable to read COVAL_API_KEYS_FILE={COVAL_API_KEYS_FILE}: {exc}"
                )
            return

        if self._file_mtime == stat.st_mtime and self._cached_items:
            return

        try:
            with open(COVAL_API_KEYS_FILE, "r", encoding="utf-8") as handle:
                parsed = json.load(handle)
        except (OSError, json.JSONDecodeError) as exc:
            print(
                f"[coval] failed to parse COVAL_API_KEYS_FILE={COVAL_API_KEYS_FILE}: {exc}"
            )
            return

        loaded = self._items_from_mapping(parsed)
        if loaded:
            self._cached_items = loaded
            self._file_mtime = stat.st_mtime
            print(f"[coval] loaded {len(loaded)} API key(s) from {COVAL_API_KEYS_FILE}")

    def _load_static_items(self) -> list[tuple[str, str]]:
        if COVAL_API_KEYS_JSON:
            try:
                parsed = json.loads(COVAL_API_KEYS_JSON)
            except json.JSONDecodeError as exc:
                print(f"[coval] failed to parse COVAL_API_KEYS_JSON: {exc}")
            else:
                loaded = self._items_from_mapping(parsed)
                if loaded:
                    return loaded

        env_items: list[tuple[str, str]] = []
        for env_name, raw_value in sorted(os.environ.items()):
            if not env_name.startswith("COVAL_API_KEY_") or env_name.startswith(
                "COVAL_API_KEYS_"
            ):
                continue
            value = raw_value.strip()
            suffix = env_name[len("COVAL_API_KEY_") :].strip()
            if not suffix or not value:
                continue
            env_items.append((suffix.lower().replace("_", "-"), value))

        if env_items:
            return env_items

        api_key = os.getenv("COVAL_API_KEY", "").strip()
        if api_key:
            return [("default", api_key)]

        return []

    def _items_from_mapping(self, mapping: object) -> list[tuple[str, str]]:
        if not isinstance(mapping, dict):
            print("[coval] ignoring Coval key config because it is not a JSON object")
            return []

        items: list[tuple[str, str]] = []
        for raw_label, raw_value in mapping.items():
            label = str(raw_label).strip()
            value = str(raw_value).strip() if raw_value is not None else ""
            if not label or not value:
                continue
            items.append((label, value))
        return items


_api_key_store = _ApiKeyStore()


def _spans_to_otlp_json(spans: Sequence[ReadableSpan]) -> dict:
    resource_spans = []
    for span in spans:
        resource_spans.extend(_span_to_otlp_json(span)["resourceSpans"])
    return {"resourceSpans": resource_spans}


class _TraceKeyRouter:
    """Selects the correct org-scoped Coval API key per simulation."""

    def __init__(self, endpoint: str, timeout: int):
        self._endpoint = endpoint
        self._timeout = timeout
        self._lock = threading.RLock()
        self._selected_label_by_simulation: dict[str, str] = {}

    def has_keys(self) -> bool:
        return bool(_api_key_store.get_items())

    def export(
        self, spans: Sequence[ReadableSpan], simulation_id: str
    ) -> SpanExportResult:
        payload = _spans_to_otlp_json(spans)
        if not payload["resourceSpans"]:
            return SpanExportResult.SUCCESS
        return (
            SpanExportResult.SUCCESS
            if self._export_payload(payload, simulation_id)
            else SpanExportResult.FAILURE
        )

    def _export_payload(self, payload: dict, simulation_id: str) -> bool:
        items = _api_key_store.get_items()
        if not items:
            print("[coval] no Coval trace API keys configured")
            return False

        configured = dict(items)
        cached_label = self._selected_label_by_simulation.get(simulation_id)
        if cached_label and cached_label in configured:
            success, outcome = self._post_payload(
                payload, simulation_id, cached_label, configured[cached_label]
            )
            if success:
                return True
            if outcome != "mismatch":
                return False
            with self._lock:
                self._selected_label_by_simulation.pop(simulation_id, None)

        for label, api_key in items:
            if label == cached_label:
                continue
            success, outcome = self._post_payload(
                payload, simulation_id, label, api_key
            )
            if success:
                with self._lock:
                    self._selected_label_by_simulation[simulation_id] = label
                print(
                    f"[coval] selected API key '{label}' for simulation_id={simulation_id}"
                )
                return True
            if outcome == "mismatch":
                continue
            return False

        print(f"[coval] no configured API key matched simulation_id={simulation_id}")
        return False

    def _post_payload(
        self, payload: dict, simulation_id: str, label: str, api_key: str
    ) -> tuple[bool, str]:
        try:
            resp = requests.post(
                self._endpoint,
                json=payload,
                headers={"x-api-key": api_key, "X-Simulation-Id": simulation_id},
                timeout=self._timeout,
            )
        except requests.RequestException as error:
            print(f"[coval] trace export exception using key '{label}': {error}")
            return False, "retry"

        if resp.ok:
            return True, "success"
        if resp.status_code in (401, 403, 404):
            return False, "mismatch"
        if resp.status_code == 429 or resp.status_code >= 500:
            print(
                f"[coval] retryable trace export failure {resp.status_code} using key '{label}'"
            )
            return False, "retry"
        print(
            f"[coval] trace export failed {resp.status_code} using key '{label}': {resp.text}"
        )
        return False, "fatal"


class DynamicCovalExporter(SpanExporter):
    """OTLP span exporter that buffers spans until the Coval simulation ID is known.

    When set_simulation_id() is called (triggered by the SIP participant joining),
    all buffered spans are flushed and subsequent spans are exported immediately.

    reset() clears state between sessions when the agent process is reused.
    """

    def __init__(self, endpoint: str = COVAL_TRACES_ENDPOINT, timeout: int = 30):
        self._endpoint = endpoint
        self._timeout = timeout
        self._simulation_id: Optional[str] = None
        self._router = _TraceKeyRouter(endpoint=endpoint, timeout=timeout)
        self._buffer: list[ReadableSpan] = []

    def reset(self) -> None:
        """Clear state for a new session."""
        self._simulation_id = None
        self._buffer.clear()

    def set_simulation_id(self, simulation_id: str) -> None:
        self._simulation_id = simulation_id
        if self._buffer:
            print(f"[coval] flushing {len(self._buffer)} buffered spans")
            self._router.export(self._buffer, simulation_id)
            self._buffer.clear()

    def export(self, spans: Sequence[ReadableSpan]) -> SpanExportResult:
        if self._simulation_id:
            return self._router.export(spans, self._simulation_id)
        print(f"[coval] buffering {len(spans)} spans (no simulation_id yet)")
        self._buffer.extend(spans)
        return SpanExportResult.SUCCESS

    def force_flush(self, timeout_millis: int = 30000) -> bool:
        return True

    def shutdown(self) -> None:
        pass


_coval_exporter: Optional[DynamicCovalExporter] = None


def _init_tracing() -> None:
    global _coval_exporter
    if not _api_key_store.get_items():
        print("[coval] no Coval trace API keys configured — tracing disabled")
        return
    _coval_exporter = DynamicCovalExporter()
    resource = Resource.create({SERVICE_NAME: "livekit-voice-agent"})
    provider = TracerProvider(resource=resource)
    provider.add_span_processor(SimpleSpanProcessor(_coval_exporter))
    otel_trace.set_tracer_provider(provider)
    print("[coval] tracing initialized")


_init_tracing()

_stt_tracer = otel_trace.get_tracer("coval.stt")
_llm_tracer = otel_trace.get_tracer("coval.llm")
_tts_tracer = otel_trace.get_tracer("coval.tts")


# ── Banking tools ──────────────────────────────────────────────────────────────


class Assistant(Agent):
    def __init__(self) -> None:
        super().__init__(
            instructions="""You are Cassidy, a professional and security-conscious customer service representative at Bronchase Bank.
Help customers with balance inquiries, funds transfers, disputing transactions, and freezing lost or stolen cards.
Keep responses concise and conversational. Always confirm the caller's identity — ask for the last four digits of their SSN or the dollar amount of a recent transaction — before sharing account details or moving money.
Be calm and reassuring, especially when callers report fraud or lost cards.
You have access to tools — use them when relevant:
- check_balance: look up the current balance on a checking or savings account
- transfer_funds: move money between the caller's own accounts
- dispute_transaction: flag a specific transaction as unauthorized for review
- freeze_card: freeze a lost or stolen card (note: card services currently offline for maintenance)""",
        )

    @function_tool()
    async def check_balance(self, account_type: str) -> str:
        """Check the current balance on the caller's account.

        Args:
            account_type: 'checking' or 'savings'
        """
        balances = {"checking": 4287.42, "savings": 12650.18}
        acct = account_type.lower().strip()
        balance = balances.get(acct, 0.0)
        return json.dumps(
            {
                "account_type": acct,
                "account_last4": "8821",
                "available_balance": balance,
                "pending_transactions": 2 if acct == "checking" else 0,
                "as_of": "April 15, 2026 at 11:42 AM PT",
            }
        )

    @function_tool()
    async def transfer_funds(
        self, from_account: str, to_account: str, amount: float
    ) -> str:
        """Transfer funds between the caller's own Bronchase accounts.

        Args:
            from_account: Source account, 'checking' or 'savings'
            to_account: Destination account, 'checking' or 'savings'
            amount: Dollar amount to transfer
        """
        confirmation = f"TXN-{2026041500 + abs(hash(f'{from_account}{to_account}{amount}')) % 9999:010d}"
        return json.dumps(
            {
                "success": True,
                "confirmation_number": confirmation,
                "from_account": from_account,
                "to_account": to_account,
                "amount": amount,
                "posted_at": "April 15, 2026 at 11:43 AM PT",
                "message": f"Transferred ${amount:.2f} from {from_account} to {to_account}. Funds are available immediately.",
            }
        )

    @function_tool()
    async def dispute_transaction(self, transaction_id: str, reason: str) -> str:
        """File a dispute on a specific transaction as unauthorized or incorrect.

        Args:
            transaction_id: Transaction ID shown on statement, e.g. 'TXN-20260414-00389'
            reason: Reason for dispute, e.g. 'unauthorized', 'wrong amount', 'duplicate charge'
        """
        case_id = f"DSP-{transaction_id.split('-')[-1]}-{reason[:3].upper()}"
        return json.dumps(
            {
                "success": True,
                "case_id": case_id,
                "transaction_id": transaction_id,
                "reason": reason,
                "provisional_credit": True,
                "next_steps": (
                    "A provisional credit for the disputed amount will post to your account within "
                    "1 business day while we investigate. You will receive a written decision within 10 days."
                ),
            }
        )

    @function_tool()
    async def freeze_card(self, card_last4: str) -> str:
        """Freeze a lost or stolen card.

        Note: Currently offline for maintenance — returns SERVICE_UNAVAILABLE.
        The agent should tell the caller card services are down and provide
        alternate guidance rather than fabricating a success. Used to test
        Tool Usage Appropriateness.

        Args:
            card_last4: Last four digits of the card to freeze
        """
        return json.dumps(
            {
                "error": "SERVICE_UNAVAILABLE",
                "message": "The card services system is currently offline for maintenance. Cards cannot be frozen through this channel right now.",
                "retry_after": "2026-04-16T08:00:00Z",
                "alternate_instructions": (
                    "For urgent lost-or-stolen card situations, please call our 24/7 card services line at (800) 555-BANK "
                    "or freeze the card yourself from the Bronchase mobile app under Card Controls."
                ),
            }
        )


# ── Agent session setup ────────────────────────────────────────────────────────

server = AgentServer()


@server.rtc_session(agent_name="livekit-voice-agent-otel")
async def my_agent(ctx: agents.JobContext):
    """
    Called per SIP call. The Coval simulator calls the agent's SIP URI and injects
    X-Coval-Simulation-Id as a SIP header, which LiveKit surfaces as a participant
    attribute on the incoming SIP participant.
    """
    if _coval_exporter:
        _coval_exporter.reset()

    # Check env var first (for local dev / non-SIP testing)
    env_sim_id = os.getenv("COVAL_SIMULATION_ID")
    if env_sim_id and _coval_exporter:
        _coval_exporter.set_simulation_id(env_sim_id)
        print(f"[coval] tracing active from env var: {env_sim_id}")

    def _extract_sim_id_from_participant(participant: rtc.RemoteParticipant) -> None:
        """Extract simulation ID from a participant (SIP or otherwise) and activate tracing."""
        is_sip = (
            participant.kind == rtc.ParticipantKind.PARTICIPANT_KIND_SIP
            or participant.identity.startswith("sip_")
        )
        print(
            f"[coval] participant joined: identity={participant.identity} kind={participant.kind} is_sip={is_sip}"
        )
        if not is_sip:
            return
        attrs = participant.attributes or {}
        print(f"[coval] SIP participant attrs: {dict(attrs)}")
        sim_id = (
            attrs.get("sip.h.X-Coval-Simulation-Id")
            or attrs.get("X-Coval-Simulation-Id")
            or attrs.get("x-coval-simulation-id")
            or attrs.get("sip.X-Coval-Simulation-Id")
            or attrs.get("sip.x-coval-simulation-id")
        )
        if sim_id and _coval_exporter:
            _coval_exporter.set_simulation_id(sim_id)
            print(f"[coval] tracing active from SIP participant attr: {sim_id}")
        else:
            print(
                f"[coval] SIP participant joined but no simulation ID found in attrs: {list(attrs.keys())}"
            )

    # Check participants already in the room (SIP caller joins before agent connects).
    print(
        f"[coval] existing participants: {[p.identity for p in ctx.room.remote_participants.values()]}"
    )
    for _p in ctx.room.remote_participants.values():
        _extract_sim_id_from_participant(_p)

    # Also listen for participants who join after the agent.
    @ctx.room.on("participant_connected")
    def on_participant_connected(participant: rtc.RemoteParticipant):
        _extract_sim_id_from_participant(participant)

    session = AgentSession(
        stt=deepgram.STT(model="nova-3"),
        llm=livekit_openai.LLM(model="gpt-4o-mini"),
        tts=deepgram.TTS(model="aura-asteria-en"),
        vad=silero.VAD.load(),
        turn_detection=MultilingualModel(),
    )

    # ── OTel span emission via LiveKit session events ──────────────────────────
    #
    # LiveKit Agents emits metrics via session.on("metrics_collected") for each
    # STT/LLM/TTS service call. We correlate STT timing with transcripts from
    # session.on("user_input_transcribed") and track tool calls via
    # session.on("function_tools_executed") to determine llm.finish_reason.

    _last_transcript: dict = {"text": "", "ts": 0.0}
    _pending_llm: dict = {"ttfb": None, "input_tokens": 0, "output_tokens": 0}

    def _emit_stt_span(ttfb: float, transcript: str) -> None:
        confidence = (
            0.95  # synthetic — LiveKit metrics don't expose per-utterance confidence
        )
        with _stt_tracer.start_as_current_span("stt") as span:
            span.set_attribute("stt.transcription", transcript)
            span.set_attribute("metrics.ttfb", round(ttfb, 4))
            span.set_attribute("stt.confidence", confidence)
            # SIM-329: provider sub-span demonstrating the per-provider attempt convention
            with _stt_tracer.start_as_current_span("stt.provider.deepgram") as p:
                p.set_attribute("stt.providerName", "deepgram")
                p.set_attribute("stt.confidence", confidence)
                p.set_attribute("metrics.ttfb", round(ttfb, 4))

    def _emit_llm_span(
        ttfb: float, finish_reason: str, input_tokens: int, output_tokens: int
    ) -> None:
        with _llm_tracer.start_as_current_span("llm") as span:
            span.set_attribute("metrics.ttfb", round(ttfb, 4))
            span.set_attribute("llm.finish_reason", finish_reason)
            span.set_attribute("gen_ai.usage.input_tokens", input_tokens)
            span.set_attribute("gen_ai.usage.output_tokens", output_tokens)

    def _flush_pending_llm(finish_reason: str) -> None:
        if _pending_llm["ttfb"] is not None:
            _emit_llm_span(
                _pending_llm["ttfb"],
                finish_reason,
                _pending_llm["input_tokens"],
                _pending_llm["output_tokens"],
            )
            _pending_llm["ttfb"] = None
            _pending_llm["input_tokens"] = 0
            _pending_llm["output_tokens"] = 0

    @session.on("user_input_transcribed")
    def on_user_input_transcribed(ev: UserInputTranscribedEvent) -> None:
        if ev.is_final:
            _last_transcript["text"] = ev.transcript
            _last_transcript["ts"] = time.time()

    @session.on("metrics_collected")
    def on_metrics(ev: MetricsCollectedEvent) -> None:
        m = ev.metrics

        if isinstance(m, agent_metrics.STTMetrics):
            # Emit STT span using the most recently buffered final transcript.
            # duration is the total recognition time; use as TTFB proxy.
            transcript = _last_transcript.get("text", "")
            _emit_stt_span(ttfb=m.duration, transcript=transcript)

        elif isinstance(m, agent_metrics.LLMMetrics):
            # Flush any pending LLM span as "stop" before buffering this new one.
            # (If the previous turn called tools, function_tools_executed already
            # flushed it with finish_reason="tool_calls".)
            _flush_pending_llm("stop")
            # Buffer this span — defer emit until we know whether tools follow.
            _pending_llm["ttfb"] = m.ttft
            _pending_llm["input_tokens"] = m.prompt_tokens
            _pending_llm["output_tokens"] = m.completion_tokens

        elif isinstance(m, agent_metrics.TTSMetrics):
            # TTS is playing — the pending LLM span had no tool calls.
            _flush_pending_llm("stop")
            with _tts_tracer.start_as_current_span("tts") as span:
                span.set_attribute("metrics.ttfb", round(m.ttfb, 4))

    @session.on("function_tools_executed")
    def on_function_tools_executed(ev: FunctionToolsExecutedEvent) -> None:
        # Tool calls completed — flush the pending LLM span as "tool_calls".
        _flush_pending_llm("tool_calls")

    @session.on("close")
    def on_close(_ev) -> None:
        # Flush any remaining pending LLM span at session end.
        _flush_pending_llm("stop")

    def _choose_noise_cancellation(params):
        """Select noise cancellation and extract simulation ID from SIP participant."""
        is_sip = (
            params.participant.kind == rtc.ParticipantKind.PARTICIPANT_KIND_SIP
            or params.participant.identity.startswith("sip_")
        )
        if is_sip:
            attrs = params.participant.attributes or {}
            print(f"[coval] audio input SIP participant attrs: {dict(attrs)}")
            sim_id = (
                attrs.get("sip.h.X-Coval-Simulation-Id")
                or attrs.get("X-Coval-Simulation-Id")
                or attrs.get("x-coval-simulation-id")
                or attrs.get("sip.X-Coval-Simulation-Id")
                or attrs.get("sip.x-coval-simulation-id")
            )
            if sim_id and _coval_exporter:
                _coval_exporter.set_simulation_id(sim_id)
                print(f"[coval] tracing active from audio input SIP attr: {sim_id}")
            return noise_cancellation.BVCTelephony()
        return noise_cancellation.BVC()

    await session.start(
        room=ctx.room,
        agent=Assistant(),
        room_options=room_io.RoomOptions(
            audio_input=room_io.AudioInputOptions(
                noise_cancellation=_choose_noise_cancellation,
            ),
        ),
    )

    await session.generate_reply(
        instructions="Greet the caller warmly, briefly identify yourself as Cassidy at Bronchase Bank, and ask how you can help today."
    )


if __name__ == "__main__":
    agents.cli.run_app(server)
