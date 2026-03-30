import random
from datetime import datetime

from dotenv import load_dotenv

from livekit import agents, rtc
from livekit.agents import AgentServer, AgentSession, Agent, function_tool, room_io
from livekit.plugins import noise_cancellation, openai, silero
from livekit.plugins.turn_detector.multilingual import MultilingualModel

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
            results = [
                {"title": r["title"], "url": r["href"], "snippet": r["body"]}
                for r in raw
            ]
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
    session = AgentSession(
        stt=openai.STT(model="whisper-1"),
        llm=openai.LLM(model="gpt-4o-mini"),
        tts=openai.TTS(model="tts-1"),
        vad=silero.VAD.load(),
        turn_detection=MultilingualModel(),
    )

    await session.start(
        room=ctx.room,
        agent=Assistant(),
        room_options=room_io.RoomOptions(
            audio_input=room_io.AudioInputOptions(
                noise_cancellation=lambda params: (
                    noise_cancellation.BVCTelephony()
                    if params.participant.kind
                    == rtc.ParticipantKind.PARTICIPANT_KIND_SIP
                    else noise_cancellation.BVC()
                ),
            ),
        ),
    )

    await session.generate_reply(
        instructions="Greet the user as a helpful voice assistant and offer your assistance."
    )


if __name__ == "__main__":
    agents.cli.run_app(server)
