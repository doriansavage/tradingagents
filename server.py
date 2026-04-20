"""
FastAPI server exposing TradingAgents (tauricresearch/tradingagents) pour DodoCrypto.
Port : 8001
Endpoints :
  POST /trading-agents/run        → réponse JSON complète (batch)
  POST /trading-agents/stream     → SSE, un event par étape du graph
"""

import concurrent.futures
import json
from datetime import date, timedelta
from typing import AsyncGenerator, List, Optional

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

load_dotenv()

from tradingagents.graph.trading_graph import TradingAgentsGraph
from tradingagents.default_config import DEFAULT_CONFIG

app = FastAPI(title="DodoCrypto TradingAgents", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


class RunRequest(BaseModel):
    ticker: str
    trade_date: Optional[str] = None
    analysts: Optional[List[str]] = None
    analysts_preset: Optional[str] = None  # "standard" | "fast"
    llm_provider: Optional[str] = "openai"
    deep_think_llm: Optional[str] = "gpt-4o"
    quick_think_llm: Optional[str] = "gpt-4o-mini"
    max_debate_rounds: Optional[int] = 1
    max_risk_discuss_rounds: Optional[int] = 1
    output_language: Optional[str] = "French"
    openai_reasoning_effort: Optional[str] = None
    anthropic_effort: Optional[str] = None
    google_thinking_level: Optional[str] = None
    timeout_seconds: Optional[int] = 180


def build_config(req: RunRequest) -> dict:
    config = DEFAULT_CONFIG.copy()
    config["llm_provider"]            = req.llm_provider
    config["deep_think_llm"]          = req.deep_think_llm
    config["quick_think_llm"]         = req.quick_think_llm
    config["max_debate_rounds"]       = req.max_debate_rounds
    config["max_risk_discuss_rounds"] = req.max_risk_discuss_rounds
    config["output_language"]         = req.output_language
    # backend_url default in DEFAULT_CONFIG est OpenAI — invalide pour Anthropic/Google.
    # On le force à None pour les providers non-OpenAI afin que les SDK utilisent leur URL par défaut.
    if req.llm_provider and req.llm_provider.lower() != "openai":
        config["backend_url"] = None
    config["data_vendors"] = {
        "core_stock_apis":      "yfinance",
        "technical_indicators": "yfinance",
        "fundamental_data":     "yfinance",
        "news_data":            "yfinance",
    }
    if req.openai_reasoning_effort:
        config["openai_reasoning_effort"] = req.openai_reasoning_effort
    if req.anthropic_effort:
        config["anthropic_effort"] = req.anthropic_effort
    if req.google_thinking_level:
        config["google_thinking_level"] = req.google_thinking_level
    return config


def sse(event: str, data: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


# Noms lisibles pour chaque clé d'état
STEP_LABELS = {
    "market_report":       "Analyse de marché terminée",
    "fundamentals_report": "Analyse fondamentale terminée",
    "sentiment_report":    "Analyse du sentiment terminée",
    "news_report":         "Analyse des news terminée",
    "investment_debate_state": "Débat haussier / baissier terminé",
    "risk_debate_state":   "Évaluation du risque terminée",
    "final_trade_decision":"Décision du Portfolio Manager",
}


@app.get("/health")
def health():
    return {"status": "ok"}


def _resolve_analysts(req: RunRequest) -> List[str]:
    """Résout la liste des analysts depuis analysts_preset ou analysts explicites."""
    if req.analysts:
        return req.analysts
    if req.analysts_preset == "fast":
        return ["fundamentals", "news"]
    # "standard" ou non spécifié
    return ["market", "fundamentals", "social", "news"]


@app.post("/trading-agents/run")
def run(req: RunRequest):
    """Réponse JSON complète — attend la fin de l'analyse."""
    trade_date = req.trade_date or str(date.today() - timedelta(days=1))
    analysts   = _resolve_analysts(req)
    timeout    = req.timeout_seconds or 180

    def _do_run():
        ta = TradingAgentsGraph(selected_analysts=analysts, debug=True, config=build_config(req))
        return ta.propagate(req.ticker, trade_date)

    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(_do_run)
            try:
                final_state, decision = future.result(timeout=timeout)
            except concurrent.futures.TimeoutError:
                raise HTTPException(
                    status_code=408,
                    detail=f"TradingAgents timeout après {timeout}s — essayez analysts_preset='fast' ou augmentez timeout_seconds",
                )
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

    return _format_result(req.ticker, trade_date, decision, final_state)


@app.post("/trading-agents/stream")
async def stream(req: RunRequest):
    """SSE — émet un event à chaque étape complétée du graph."""
    trade_date = req.trade_date or str(date.today() - timedelta(days=1))
    analysts   = _resolve_analysts(req)

    async def generate() -> AsyncGenerator[str, None]:
        try:
            ta = TradingAgentsGraph(
                selected_analysts=analysts,
                debug=False,
                config=build_config(req),
            )

            # Initialisation
            init_state = ta.propagator.create_initial_state(req.ticker, trade_date)
            graph_args = ta.propagator.get_graph_args()
            prev = {}

            yield sse("start", {"ticker": req.ticker, "trade_date": trade_date})

            # Itère sur chaque étape du graph
            for chunk in ta.graph.stream(init_state, **graph_args):
                for key, value in chunk.items():
                    if key in STEP_LABELS and value and value != prev.get(key):
                        prev[key] = value

                        # Extraire le texte selon la clé
                        if key == "investment_debate_state":
                            content = value.get("judge_decision") or value.get("history", "")
                        elif key == "risk_debate_state":
                            content = value.get("judge_decision") or value.get("history", "")
                        else:
                            content = value if isinstance(value, str) else ""

                        if content:
                            yield sse("step", {
                                "key":   key,
                                "label": STEP_LABELS[key],
                                "content": content,
                            })

            # Étape finale : décision
            final_state = prev if prev else chunk
            # reconstruire l'état final complet
            ta.curr_state = chunk
            decision = ta.process_signal(chunk.get("final_trade_decision", ""))

            yield sse("complete", _format_result(req.ticker, trade_date, decision, chunk))

        except Exception as e:
            yield sse("error", {"detail": str(e)})

    return StreamingResponse(generate(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


def _format_result(ticker, trade_date, decision, state) -> dict:
    return {
        "ticker":              ticker,
        "trade_date":          trade_date,
        "decision":            decision,
        "final_report":        state.get("final_trade_decision", ""),
        "market_report":       state.get("market_report", ""),
        "fundamentals_report": state.get("fundamentals_report", ""),
        "sentiment_report":    state.get("sentiment_report", ""),
        "news_report":         state.get("news_report", ""),
        "bull_thesis":         state.get("investment_debate_state", {}).get("bull_history", ""),
        "bear_thesis":         state.get("investment_debate_state", {}).get("bear_history", ""),
        "risk_assessment":     state.get("risk_debate_state", {}).get("history", ""),
    }


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8001, reload=False)
