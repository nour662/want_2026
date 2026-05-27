from app.core.db.base import engine, Base
from app.core.db.init_db import *

def create_tables():
    Base.metadata.create_all(bind=engine)
    print("All tables created successfully!")

if __name__ == "__main__":
    create_tables()
