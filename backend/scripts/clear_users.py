"""
Script to clear all data from the users table and related tables.
"""
import sys
from pathlib import Path

# Add the backend directory to the path
backend_dir = Path(__file__).parent.parent
sys.path.insert(0, str(backend_dir))

from sqlalchemy.orm import Session
from app.core.db.base import SessionLocal
from app.models.users import User, HubUserRole


def clear_users_data():
    """Clear all data from users and related tables."""
    db: Session = SessionLocal()
    try:
        # Delete hub_user_roles first (foreign key dependency)
        hub_roles_count = db.query(HubUserRole).count()
        db.query(HubUserRole).delete()
        print(f"Deleted {hub_roles_count} records from hub_user_roles table")
        
        # Delete users
        users_count = db.query(User).count()
        db.query(User).delete()
        print(f"Deleted {users_count} records from users table")
        
        # Commit the changes
        db.commit()
        print("\n✓ Successfully cleared all user data from the database")
        
    except Exception as e:
        db.rollback()
        print(f"\n✗ Error clearing user data: {e}")
        raise
    finally:
        db.close()


if __name__ == "__main__":
    print("Starting to clear users data...")
    clear_users_data()
