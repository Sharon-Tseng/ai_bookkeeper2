import os
import base64
from langchain_google_genai import ChatGoogleGenerativeAI
from langchain.agents import create_agent
from agent_tools import write_to_sheets
from io import BytesIO
from dotenv import load_dotenv
from datetime import datetime

# Load environment variables from .env file
load_dotenv()
# Import API key
if os.getenv("GOOGLE_API_KEY"):
    os.environ["GOOGLE_API_KEY"] = os.getenv("GOOGLE_API_KEY")
else:
    print("Warning: GOOGLE_API_KEY not set. Using dummy key.")
    os.environ["GOOGLE_API_KEY"] = "DUMMY_KEY"

gemini_llm = ChatGoogleGenerativeAI(
    model="gemini-2.5-flash", 
    temperature=0,
    include_thoughts = True)

bookkeeper_graph_agent = create_agent(
    gemini_llm, 
    tools = [write_to_sheets],
    system_prompt="你是一個專業的記帳助手。請分析圖片並提取日期、項目、金額、分類。"
)

class AccountAgents:
    @staticmethod
    def run_bookkeeper(image_data, spreadsheet_id):
        buffered = BytesIO()
        image_data.save(buffered, format="PNG")
        base64_image = base64.b64encode(buffered.getvalue()).decode("utf-8")

        # create unique thought_signature for sessions
        current_sig = f"bookkeeper-{spreadsheet_id}-{datetime.now().strftime('%Y%m%d%H%M%S')}"

        inputs = {
            "messages": [
                ("user",
                 [{"type": "text", "text": 
                   # Prompting technique: https://chichieh-huang.com/posts/6ac4201a4cbe/ 
                   """
                   請分析這張收據照片，提取日期、項目、金額、分類，並使用 write_to_sheets 工具存入 Google Sheets。
                   日期可能分為以下兩種:
                   1. 格式為 YYYY/MM/DD 或 YYYY-MM-DD，例如 2024/01/31 或 2024-01-31
                   2. 格式為 YYYY/MM 或 YYYY-MM，例如 2024/01 或 2024-01
                   3. 格式為 DD/MM/YYYY 或 DD-MM-YYYY，例如 31/01/2024 或 31-01-2024
                   如果不確認日期，請使用今天的日期。
                   項目是收據上購買的商品或服務名稱，若出現多個項目，請選擇主要項目，其他為細項。
                   分類請根據常見消費類別進行分類:健身/健康、日常購物、餐飲/飲品、交通，若無法判斷，請歸類為「其他」。
                   如果無法從圖片提取任何文字資訊，請回覆「無法識別圖片中的文字資訊，請提供清晰的收據圖片。」。
                   """},
                  {"type": "image_url", 
                   "image_url": {"url": f"data:image/png;base64,{base64_image}"}
                  }])
            ]
        }

        config = {
            "configurable": {"SPREADSHEET_ID": spreadsheet_id}
        }

        try:
            response = bookkeeper_graph_agent.invoke(inputs, config=config)
            return response["messages"][-1].content 
        except Exception as e:
            return f"Error during agent execution: {str(e)}"
