import types

from ensemble.delegate import composer

from delegate.tests.case import Case
from delegate.tests.unit.composer.shipped import (
    API,
    DROPPED,
)


class Package(Case):
    def test_the_api_is_the_modules_names(self):
        self.assertEqual(composer.__all__, API)
        self.assertEqual([name for name in DROPPED if hasattr(composer, name)], [])
        self.assertIsInstance(composer.teams, types.ModuleType)
