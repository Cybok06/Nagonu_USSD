from pymongo.mongo_client import MongoClient
from pymongo.server_api import ServerApi
from urllib.parse import quote_plus

# MongoDB Atlas URI (URL-encode credentials to handle special chars)
username = "zico_cybok"
password = "T7uF10RDgC5Im7Wp"
uri = (
    f"mongodb+srv://{quote_plus(username)}:{quote_plus(password)}"
    "@cluster0.a77dwo1.mongodb.net/?retryWrites=true&w=majority&appName=Cluster0"
)

# Create client with stable API version
client = MongoClient(uri, server_api=ServerApi('1'))

# Try to connect and ping the cluster
try:
    client.admin.command('ping')
    print("Pinged your deployment. You successfully connected to MongoDB!")
except Exception as e:
    print("MongoDB connection error:", e)

# Access your databases
db = client["zico"]
