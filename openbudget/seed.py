"""Idempotent demo dataset; invoke manually (never auto-seeded in production)."""
from .db import record_audit
from .security import hash_password

DEMO_PASSWORD = "DemoPass123!"


def seed(db):
    if db.execute("SELECT COUNT(*) FROM cycles").fetchone()[0]:
        return False
    db.execute("BEGIN IMMEDIATE")
    try:
        people = [
            ("Mira Patel", "mira@demo.local", "citizen"),
            ("Aadi Rao", "aadi@demo.local", "citizen"),
            ("Sana Khan", "sana@demo.local", "citizen"),
            ("Alex Morgan", "moderator@demo.local", "moderator"),
        ]
        people += [(f"Demo Citizen {i:02d}", f"citizen{i:02d}@demo.local", "citizen") for i in range(4, 17)]
        for name, email, role in people:
            db.execute("INSERT INTO users(name,email,password_hash,role) VALUES(?,?,?,?)",
                       (name, email, hash_password(DEMO_PASSWORD), role))
        user_ids = [x[0] for x in db.execute("SELECT id FROM users WHERE role='citizen' ORDER BY id")]
        mod_id = db.execute("SELECT id FROM users WHERE role='moderator'").fetchone()[0]
        cycles = [
            ("neighborhood-2027", "Neighborhood Futures", 2027,
             "Bring bold ideas to life. Submit community improvements for our next budget.", 15_000_000, "submissions"),
            ("community-2026", "Community Priorities", 2026,
             "Vote for the projects you believe will make the biggest difference.", 12_000_000, "voting"),
            ("impact-2025", "Our Shared Impact", 2025,
             "See where the community's votes turned into funded commitments.", 10_000_000, "allocated"),
        ]
        for cycle in cycles:
            db.execute("INSERT INTO cycles(slug,name,year,description,total_budget,stage) VALUES(?,?,?,?,?,?)", cycle)
        c = {r["slug"]: r["id"] for r in db.execute("SELECT id,slug FROM cycles")}
        def proposal(slug, title, desc, category, district, amount, status, owner=0):
            cursor = db.execute("""INSERT INTO proposals(cycle_id,user_id,title,description,category,district,
                requested_amount,status) VALUES(?,?,?,?,?,?,?,?)""",
                (c[slug], user_ids[owner], title, desc, category, district, amount, status))
            return cursor.lastrowid
        c27 = "neighborhood-2027"
        proposal(c27, "Rain gardens for safer streets",
                 "Create planted rain gardens along flood-prone junctions to capture runoff, improve biodiversity and make walking safer during monsoons.",
                 "Environment", "North Ward", 2_100_000, "approved")
        proposal(c27, "Accessible public reading corners",
                 "Convert underused corners of parks into shaded, wheelchair-accessible reading spaces with books, benches and solar lighting.",
                 "Education", "Central Ward", 1_250_000, "approved", 1)
        proposal(c27, "Shade structures for neighborhood playgrounds",
                 "Install weather-resistant shade structures, drinking water points and accessible benches around popular children's play areas.",
                 "Public spaces", "West Ward", 1_800_000, "pending", 2)
        proposal(c27, "Community composting gardens",
                 "Set up small managed compost hubs and hands-on learning plots at three community parks to reduce organic waste.",
                 "Environment", "East Ward", 1_100_000, "pending", 1)
        c26 = "community-2026"
        p26 = [
            proposal(c26, "Safer crossings near schools", "Add raised pedestrian crossings, tactile markings and clear signals near high-traffic schools to make daily commutes safer.", "Mobility", "Central Ward", 3_000_000, "approved"),
            proposal(c26, "Pocket parks in every ward", "Turn neglected vacant public land into small shaded green spaces with benches and indigenous plants.", "Public spaces", "North Ward", 4_200_000, "approved", 1),
            proposal(c26, "Digital skills community labs", "Create open-access digital learning labs with equipment and free introductory sessions for job seekers and students.", "Education", "East Ward", 2_400_000, "approved", 2),
            proposal(c26, "LED lighting for walkways", "Replace dark walkway lighting with efficient LED fixtures and publish a maintenance schedule.", "Infrastructure", "West Ward", 3_600_000, "approved"),
            proposal(c26, "Rainwater harvesting at public buildings", "Install roof-water collection and monitoring systems in community buildings to reduce water waste.", "Environment", "South Ward", 1_950_000, "approved", 1),
            proposal(c26, "Neighborhood cycling network", "Mark a connected network of protected short-distance cycling links near transit stops and public markets.", "Mobility", "South Ward", 3_300_000, "approved", 2),
        ]
        c25 = "impact-2025"
        p25 = [
            proposal(c25, "Clean drinking water stations", "Install filtered drinking water points in municipal parks with scheduled quality inspections and public maintenance logs.", "Health", "Central Ward", 4_200_000, "funded"),
            proposal(c25, "Solar lights for community lanes", "Install solar-powered pathway lighting in poorly lit neighborhood lanes, prioritizing routes used by older residents.", "Infrastructure", "North Ward", 3_600_000, "funded", 1),
            proposal(c25, "Tree canopy restoration", "Plant native shade trees and fund two years of maintenance, watering and community stewardship.", "Environment", "West Ward", 3_000_000, "unfunded", 2),
            proposal(c25, "Inclusive play equipment", "Add inclusive play structures and safer surfaces in two public playgrounds.", "Public spaces", "East Ward", 1_400_000, "funded", 1),
            proposal(c25, "Open air art walls", "Provide designated community mural walls and materials for local artist workshops.", "Culture", "South Ward", 1_200_000, "unfunded", 2),
        ]
        # One ballot per resident per cycle. Distribution is intentionally varied.
        for idx, user_id in enumerate(user_ids):
            db.execute("INSERT INTO votes(cycle_id,proposal_id,user_id) VALUES(?,?,?)",
                       (c[c26], p26[[0,0,0,0,0,1,1,1,1,2,2,2,3,3,4,5][idx]], user_id))
            db.execute("INSERT INTO votes(cycle_id,proposal_id,user_id) VALUES(?,?,?)",
                       (c[c25], p25[[0,0,0,0,0,0,1,1,1,1,1,2,2,3,3,4][idx]], user_id))
        for proposal_id, amount in [(p25[0], 4_200_000), (p25[1], 3_600_000), (p25[3], 1_400_000)]:
            db.execute("INSERT INTO allocations(cycle_id,proposal_id,amount) VALUES(?,?,?)",
                       (c[c25], proposal_id, amount))
        record_audit(db, mod_id, c[c25], "cycle", c[c25], "allocated", "Demo historical allocation")
        db.execute("COMMIT")
        return True
    except Exception:
        db.execute("ROLLBACK")
        raise
