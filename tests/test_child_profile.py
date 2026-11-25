from backend.services import child_profile as cp


def test_tags_parsed_stripped_and_validated():
    clean, found = cp.parse_tags("अरे वाह रवि! तुम पाँच साल के हो?\n<<name=Ravi Kumar>> <<age=5 years>> <<likes=mangoes>>")
    assert clean == "अरे वाह रवि! तुम पाँच साल के हो?" and "<<" not in clean
    m = cp.apply_updates({}, found)
    assert m == {"name": "Ravi", "age": 5, "likes": ["mangoes"]}  # first name only


def test_private_values_rejected():
    _, found = cp.parse_tags("ok <<name=9876543210>> <<likes=12 MG Road>> <<age=45>>")
    assert cp.apply_updates({}, found) == {}


def test_stages_progress():
    dev = {"child_name": None, "child_age": None, "child_interests": None}
    assert cp.stage_of(cp.effective_profile(dev, {})) == "know"
    assert cp.stage_of(cp.effective_profile(dev, {"name": "Anu", "turns": 2})) == "friend"
    assert cp.stage_of(cp.effective_profile(dev, {"name": "Anu", "age": 5, "likes": ["cats"], "turns": 5})) == "learn"
    # parent-entered values count too
    dev2 = {"child_name": "Chinnu", "child_age": 6, "child_interests": "cricket"}
    assert cp.stage_of(cp.effective_profile(dev2, {"turns": 6})) == "learn"


def test_memory_saved_per_device_and_resettable(client):
    client.patch("/api/devices/virtual-toy-01", json={"reset_child_memory": True, "child_name": "Chinnu", "child_age": 6})
    client.post("/api/conversation/virtual-toy-01/text", json={"text": "hello"})
    d = client.get("/api/devices/virtual-toy-01").json()
    assert d["child_name"] == "Chinnu" and d["child_memory"]["turns"] == 1
    client.patch("/api/devices/virtual-toy-01", json={"reset_child_memory": True, "child_name": "", "child_age": 0})
    d = client.get("/api/devices/virtual-toy-01").json()
    assert d["child_memory"] == {} and d["child_name"] is None and d["child_age"] is None
