import random
from datetime import datetime
import asyncio  # COVAL: Import asyncio

from dotenv import load_dotenv

from livekit import agents, rtc
from livekit.agents import AgentServer, AgentSession, Agent, function_tool, room_io
from livekit.agents.stt import FallbackAdapter as STTFallbackAdapter, AvailabilityChangedEvent
from livekit.plugins import deepgram, noise_cancellation, silero
try:
    from livekit.plugins import google as google_stt
    _HAS_GOOGLE_STT = True
except ImportError:
    _HAS_GOOGLE_STT = False
from livekit.plugins.turn_detector.multilingual import MultilingualModel

from coval_tracing import setup_coval_tracing, set_simulation_id, instrument_session, set_active_stt_provider

load_dotenv(".env.local")

_WEATHER_CONDITIONS = ["sunny", "cloudy", "partly cloudy", "rainy", "windy", "foggy"]
_ORDER_STATUSES = ["processing", "shipped", "out for delivery", "delivered", "delayed"]


class Assistant(Agent):
    def __init__(self) -> None:
        super().__init__(
            instructions="""You are a helpful voice assistant used for testing Coval's voice agent evaluation platform.
Keep your responses concise and conversational. Be friendly and helpful.
You have access to tools — use them when relevant.""",
        )

    @function_tool()
    async def get_current_time(self) -> dict:
        """Returns the current date and time."""
        now = datetime.now()
        return {"time": now.strftime("%I:%M %p"), "date": now.strftime("%A, %B %d, %Y")}

    @function_tool()
    async def get_weather(self, city: str) -> dict:
        """Returns the current weather for a given city.

        Args:
            city: The name of the city, e.g. 'San Francisco'
        """
        return {
            "city": city,
            "temperature_f": random.randint(45, 95),
            "condition": random.choice(_WEATHER_CONDITIONS),
            "humidity_pct": random.randint(30, 90),
        }

    @function_tool()
    async def search_web(self, query: str, max_results: int = 3) -> dict:
        """Search the web for up-to-date information on any topic.

        Args:
            query: The search query
            max_results: Maximum number of results to return (1-5, default 3)
        """
        from duckduckgo_search import DDGS
        max_results = min(int(max_results), 5)
        try:
            ddgs = DDGS()
            raw = list(ddgs.text(query, max_results=max_results))
            results = [{"title": r["title"], "url": r["href"], "snippet": r["body"]} for r in raw]
            return {"query": query, "results": results}
        except Exception as e:
            return {"query": query, "error": str(e), "results": []}

    @function_tool()
    async def lookup_order_status(self, order_id: str) -> dict:
        """Looks up the status of an order by order ID.

        Args:
            order_id: The order ID to look up, e.g. 'ORD-12345'
        """
        return {
            "order_id": order_id,
            "status": random.choice(_ORDER_STATUSES),
            "estimated_delivery": "Mar 1, 2026",
            "carrier": random.choice(["UPS", "FedEx", "USPS", "DHL"]),
        }


server = AgentServer()

@server.rtc_session(agent_name="livekit-voice-agent-otel")
async def my_agent(ctx: agents.JobContext):
    setup_coval_tracing()  # COVAL: Setting up Coval tracing

    async def _check_sim_id(participant):  # COVAL: Check for simulation ID
        sim_id = participant.attributes.get("sip.h.X-Coval-Simulation-Id")
        if sim_id:
            set_simulation_id(sim_id)

    ctx.room.on("participant_connected", lambda p: asyncio.ensure_future(_check_sim_id(p)))  # COVAL: Listener for participant connected
    ctx.room.on("participant_attributes_changed", lambda old, p: asyncio.ensure_future(_check_sim_id(p)))  # COVAL: Listener for participant attributes changed

    # STT with fallback: Deepgram (primary) → Google (fallback)
    vad = silero.VAD.load()
    primary_stt = deepgram.STT(model="nova-3")
    if _HAS_GOOGLE_STT:
        fallback_stt = google_stt.STT(model="latest_long", language="en-US")
        stt = STTFallbackAdapter([primary_stt, fallback_stt], vad=vad)

        def _on_stt_availability_changed(ev: AvailabilityChangedEvent):
            provider = getattr(ev.stt, "provider", "unknown")
            if not ev.available:
                # Provider went down — the next successful result is from the other one
                set_active_stt_provider("google" if provider == "deepgram" else "deepgram")
            else:
                # Provider recovered — switch back to primary
                if provider == "deepgram":
                    set_active_stt_provider("deepgram")

        stt.on("stt_availability_changed", _on_stt_availability_changed)
    else:
        stt = primary_stt

    session = AgentSession(
        stt=stt,
        llm="openai/gpt-4o-mini",
        tts=deepgram.TTS(model="aura-asteria-en"),
        vad=vad,
        turn_detection=MultilingualModel(),
    )

    await session.start(
        room=ctx.room,
        agent=Assistant(),
        room_options=room_io.RoomOptions(
            audio_input=room_io.AudioInputOptions(
                noise_cancellation=lambda params: noise_cancellation.BVCTelephony() if params.participant.kind == rtc.ParticipantKind.PARTICIPANT_KIND_SIP else noise_cancellation.BVC(),
            ),
        ),
    )

    instrument_session(session)  # COVAL: Instrument the session

    await session.generate_reply(
        instructions="Greet the user as a helpful voice assistant and offer your assistance."
    )


if __name__ == "__main__":
    agents.cli.run_app(server)
