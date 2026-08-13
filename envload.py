import os 


from dotenv import load_dotenv


load_dotenv()


print(os.getenv("OPENAI_BASE_URL"))
print(os.getenv("OPENAI_API_KEY"))
