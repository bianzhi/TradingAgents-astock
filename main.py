from tradingagents.graph.trading_graph import TradingAgentsGraph
from tradingagents.default_config import DEFAULT_CONFIG

from dotenv import load_dotenv

# Load environment variables from .env file
load_dotenv()

# Create a custom config
config = DEFAULT_CONFIG.copy()
config["llm_provider"] = "deepseek"       # Use DeepSeek as LLM provider
config["deep_think_llm"] = "deepseek-v4-pro"   # Deep thinking model
config["quick_think_llm"] = "deepseek-v4-flash" # Quick thinking model
config["max_debate_rounds"] = 1  # Increase debate rounds

# Configure data vendors (A-stock by default, free, no API keys needed)
# config["data_vendors"] = {
#     "core_stock_apis": "a_stock",
#     "technical_indicators": "a_stock",
#     "fundamental_data": "a_stock",
#     "news_data": "a_stock",
#     "signal_data": "a_stock",
# }

# Initialize with custom config
ta = TradingAgentsGraph(debug=True, config=config)

# forward propagate — A-stock example
_, decision = ta.propagate("688008", "2026-05-20")
print(decision)
