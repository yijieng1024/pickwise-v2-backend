"""
10c step (g)(a)'s --apply path (app/scripts/retire_customizations_10c.py) against the
test database: every customization and the 6 Apple categories go, every other category
and every laptop link stays.
"""
import json

import pytest
from sqlmodel import select

from app.laptops.customization_model import LaptopCustomization
from app.laptops.laptop_category_model import LaptopCategory
from app.scripts import retire_customizations_10c as script
from app.taxonomy.category_model import Category
from tests.integration.conftest import make_laptop

pytestmark = pytest.mark.integration


@pytest.fixture
def seeded(session, brand):
    laptops = [make_laptop(brand.id, status=s) for s in ("active", "suspended", "discontinued")]
    apple = {n: Category(name=n) for n in script.APPLE}
    kept = {n: Category(name=n) for n in (*script.PROTECTED, "Student")}  # Student: unlinked, not a target
    session.add_all([*laptops, *apple.values(), *kept.values()])
    session.flush()
    for laptop in laptops:
        session.add(LaptopCustomization(laptop_id=laptop.id, category_id=apple["memory"].id,
                                        option_name="32GB", price_add_rm=800.0))
        session.add(LaptopCustomization(laptop_id=laptop.id, category_id=apple["storage"].id,
                                        option_name="1TB", price_add_rm=800.0))
        for name in script.PROTECTED:
            session.add(LaptopCategory(laptop_id=laptop.id, category_id=kept[name].id))
    session.commit()
    return laptops, kept


def _links(session):
    return sorted((l.laptop_id, l.category_id) for l in session.exec(select(LaptopCategory)))


def test_apply_deletes_only_customizations_and_the_apple_categories(session, seeded, tmp_path):
    laptops, kept = seeded
    links = _links(session)

    result = script.run(session, write=True, backups=tmp_path)

    session.expire_all()
    assert (result["deleted"], result["removed"]) == (6, 6)
    assert session.exec(select(LaptopCustomization)).all() == []
    assert sorted(c.name for c in session.exec(select(Category))) == sorted(kept)
    assert _links(session) == links

    pre, post = sorted(tmp_path.glob("*.json"))
    pre, post = json.loads(pre.read_text()), json.loads(post.read_text())
    assert len(pre["laptop_customizations"]) == 6 and {"option_name", "price_add_rm"} <= set(pre["laptop_customizations"][0])
    assert sorted(c["name"] for c in pre["categories"]) == sorted(script.APPLE)
    assert post == {"laptop_customizations": [], "categories": []}


def test_refuses_unless_the_target_set_is_exactly_the_six(session, seeded, tmp_path):
    session.delete(session.exec(select(Category).where(Category.name == "apple_care")).one())
    session.commit()

    with pytest.raises(script.Refused, match="expected exactly"):
        script.run(session, write=True, backups=tmp_path)
    assert len(session.exec(select(LaptopCustomization)).all()) == 6
    assert list(tmp_path.iterdir()) == []
