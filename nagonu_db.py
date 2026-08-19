from pymongo.mongo_client import MongoClient
from pymongo.server_api import ServerApi

# MongoDB Atlas URI
uri = "mongodb+srv://nagonu:0500868021Yaw@cluster0.yp3zg2d.mongodb.net/?retryWrites=true&w=majority&appName=Cluster0"

# Create client with stable API version
client = MongoClient(uri, server_api=ServerApi('1'), connect=False)

# Access your databases
db = client["nagobu"]
campus_db = client["campus_data"]
