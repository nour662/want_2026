import sys
import os


sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy.orm import Session
from app.core.db.base import SessionLocal, engine, Base
from app.models.hub import Hub
from app.models.university import University
from app.models.hubs import HUBS, UNIVERSITIES

def seed_database():

    Base.metadata.create_all(bind=engine)

    db: Session = SessionLocal()

    try:

        existing_hubs = db.query(Hub).count()
        if existing_hubs > 0:
            print(f"Database already seeded with {existing_hubs} hubs. Skipping...")
            return


        print("Seeding hubs...")
        hub_map = {}
        for hub_name in HUBS:
            hub = Hub(name=hub_name)
            db.add(hub)
            db.flush()
            hub_map[hub_name] = hub.id
            print(f"  Created: {hub_name}")

        db.commit()
        print(f"✓ Created {len(HUBS)} hubs")


        print("\nSeeding universities...")
        for uni_name, hub_name in UNIVERSITIES:
            university = University(
                name=uni_name,
                hub_id=hub_map[hub_name],
                country="United States"
            )
            db.add(university)
            print(f"  Created: {uni_name} → {hub_name}")

        db.commit()
        print(f"\n✓ Created {len(UNIVERSITIES)} universities")
        print("\n✓ Database seeding complete!")

    except Exception as e:
        print(f"\n✗ Error seeding database: {e}")
        db.rollback()
        raise
    finally:
        db.close()

if __name__ == "__main__":
    seed_database()
