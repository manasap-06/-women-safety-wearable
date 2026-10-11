"""Optional database initializer. Importing main creates the required tables."""
from main import init_db

if __name__ == "__main__":
    init_db()
    print("Database tables are ready.")
