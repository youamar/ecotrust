"""Seed the DB with a small UM-faculty-style building."""
from .database import Base, engine, SessionLocal
from .models import Room


ROOMS = [
    # name, floor, dept, area, start, end, kw, tier
    ("Meeting Room 1A",  1, "Engineering",          25, 8, 20, 2.5, "full"),
    ("Meeting Room 1B",  1, "Engineering",          25, 8, 20, 2.5, "full"),
    ("Open Lab",         1, "Engineering",         120, 8, 22, 8.0, "advisory"),
    ("Seminar Hall",     2, "Education Management",180, 7, 21, 12.0, "advisory"),
    ("Office Suite 2A",  2, "Psychology",          60, 8, 19, 4.5, "full"),
    ("Office Suite 2B",  2, "Business",            60, 8, 19, 4.5, "full"),
    ("Server Room",      3, "IT",                  20, 0, 24, 6.0, "untouched"),
    ("Reading Room",     3, "Education Management",80, 8, 22, 5.0, "full"),
]


def seed():
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    db = SessionLocal()
    try:
        for name, floor, dept, area, s, e, kw, tier in ROOMS:
            db.add(Room(
                name=name, floor=floor, department=dept, area_m2=area,
                authorized_start_hour=s, authorized_end_hour=e,
                rated_kw=kw, control_tier=tier,
            ))
        db.commit()
        print(f"Seeded {len(ROOMS)} rooms.")
    finally:
        db.close()


if __name__ == "__main__":
    seed()
