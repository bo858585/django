from importlib import import_module
import os
import sys

from django.apps import apps
from django.db.migrations.recorder import MigrationRecorder
from django.db.migrations.graph import MigrationGraph
from django.utils import six
from django.conf import settings


MIGRATIONS_MODULE_NAME = 'migrations'


class MigrationLoader(object):
    """
    Loads migration files from disk, and their status from the database.

    Migration files are expected to live in the "migrations" directory of
    an app. Their names are entirely unimportant from a code perspective,
    but will probably follow the 1234_name.py convention.

    On initialisation, this class will scan those directories, and open and
    read the python files, looking for a class called Migration, which should
    inherit from django.db.migrations.Migration. See
    django.db.migrations.migration for what that looks like.

    Some migrations will be marked as "replacing" another set of migrations.
    These are loaded into a separate set of migrations away from the main ones.
    If all the migrations they replace are either unapplied or missing from
    disk, then they are injected into the main set, replacing the named migrations.
    Any dependency pointers to the replaced migrations are re-pointed to the
    new migration.

    This does mean that this class MUST also talk to the database as well as
    to disk, but this is probably fine. We're already not just operating
    in memory.
    """

    def __init__(self, connection, load=True):
        self.connection = connection
        self.disk_migrations = None
        self.applied_migrations = None
        if load:
            self.build_graph()

    @classmethod
    def migrations_module(cls, app_label):
        if app_label in settings.MIGRATION_MODULES:
            return settings.MIGRATION_MODULES[app_label]
        else:
            app_package_name = apps.get_app_config(app_label).name
            return '%s.%s' % (app_package_name, MIGRATIONS_MODULE_NAME)

    def load_disk(self):
        """
        Loads the migrations from all INSTALLED_APPS from disk.
        """
        self.disk_migrations = {}
        self.unmigrated_apps = set()
        self.migrated_apps = set()
        for app_config in apps.get_app_configs():
            if app_config.models_module is None:
                continue
            # Get the migrations module directory
            module_name = self.migrations_module(app_config.label)
            was_loaded = module_name in sys.modules
            try:
                module = import_module(module_name)
            except ImportError as e:
                # I hate doing this, but I don't want to squash other import errors.
                # Might be better to try a directory check directly.
                if "No module named" in str(e) and MIGRATIONS_MODULE_NAME in str(e):
                    self.unmigrated_apps.add(app_config.label)
                    continue
                raise
            else:
                # PY3 will happily import empty dirs as namespaces.
                if not hasattr(module, '__file__'):
                    continue
                # Module is not a package (e.g. migrations.py).
                if not hasattr(module, '__path__'):
                    continue
                # Force a reload if it's already loaded (tests need this)
                if was_loaded:
                    six.moves.reload_module(module)
            self.migrated_apps.add(app_config.label)
            directory = os.path.dirname(module.__file__)
            # Scan for .py[c|o] files
            migration_names = set()
            for name in os.listdir(directory):
                if name.endswith(".py") or name.endswith(".pyc") or name.endswith(".pyo"):
                    import_name = name.rsplit(".", 1)[0]
                    if import_name[0] not in "_.~":
                        migration_names.add(import_name)
            # Load them
            south_style_migrations = False
            for migration_name in migration_names:
                try:
                    migration_module = import_module("%s.%s" % (module_name, migration_name))
                except ImportError as e:
                    # Ignore South import errors, as we're triggering them
                    if "south" in str(e).lower():
                        south_style_migrations = True
                        break
                    raise
                if not hasattr(migration_module, "Migration"):
                    raise BadMigrationError("Migration %s in app %s has no Migration class" % (migration_name, app_config.label))
                # Ignore South-style migrations
                if hasattr(migration_module.Migration, "forwards"):
                    south_style_migrations = True
                    break
                self.disk_migrations[app_config.label, migration_name] = migration_module.Migration(migration_name, app_config.label)
            if south_style_migrations:
                self.unmigrated_apps.add(app_config.label)

    def get_migration(self, app_label, name_prefix):
        "Gets the migration exactly named, or raises KeyError"
        return self.graph.nodes[app_label, name_prefix]

    def get_migration_by_prefix(self, app_label, name_prefix):
        "Returns the migration(s) which match the given app label and name _prefix_"
        # Do the search
        results = []
        for l, n in self.disk_migrations:
            if l == app_label and n.startswith(name_prefix):
                results.append((l, n))
        if len(results) > 1:
            raise AmbiguityError("There is more than one migration for '%s' with the prefix '%s'" % (app_label, name_prefix))
        elif len(results) == 0:
            raise KeyError("There no migrations for '%s' with the prefix '%s'" % (app_label, name_prefix))
        else:
            return self.disk_migrations[results[0]]

    def build_graph(self):
        """
        Builds a migration dependency graph using both the disk and database.
        You'll need to rebuild the graph if you apply migrations. This isn't
        usually a problem as generally migration stuff runs in a one-shot process.
        """
        # Load disk data
        self.load_disk()
        # Load database data
        recorder = MigrationRecorder(self.connection)
        self.applied_migrations = recorder.applied_migrations()
        # Do a first pass to separate out replacing and non-replacing migrations
        normal = {}
        replacing = {}
        for key, migration in self.disk_migrations.items():
            if migration.replaces:
                replacing[key] = migration
            else:
                normal[key] = migration
        # Calculate reverse dependencies - i.e., for each migration, what depends on it?
        # This is just for dependency re-pointing when applying replacements,
        # so we ignore run_before here.
        reverse_dependencies = {}
        for key, migration in normal.items():
            for parent in migration.dependencies:
                reverse_dependencies.setdefault(parent, set()).add(key)
        # Carry out replacements if we can - that is, if all replaced migrations
        # are either unapplied or missing.
        for key, migration in replacing.items():
            # Ensure this replacement migration is not in applied_migrations
            self.applied_migrations.discard(key)
            # Do the check. We can replace if all our replace targets are
            # applied, or if all of them are unapplied.
            applied_statuses = [(target in self.applied_migrations) for target in migration.replaces]
            can_replace = all(applied_statuses) or (not any(applied_statuses))
            if not can_replace:
                continue
            # Alright, time to replace. Step through the replaced migrations
            # and remove, repointing dependencies if needs be.
            for replaced in migration.replaces:
                if replaced in normal:
                    # We don't care if the replaced migration doesn't exist;
                    # the usage pattern here is to delete things after a while.
                    del normal[replaced]
                for child_key in reverse_dependencies.get(replaced, set()):
                    if child_key in migration.replaces:
                        continue
                    normal[child_key].dependencies.remove(replaced)
                    normal[child_key].dependencies.append(key)
            normal[key] = migration
            # Mark the replacement as applied if all its replaced ones are
            if all(applied_statuses):
                self.applied_migrations.add(key)
        # Finally, make a graph and load everything into it
        self.graph = MigrationGraph()
        for key, migration in normal.items():
            self.graph.add_node(key, migration)
        for key, migration in normal.items():
            for parent in migration.dependencies:
                self.graph.add_dependency(key, parent)

    def detect_conflicts(self):
        """
        Looks through the loaded graph and detects any conflicts - apps
        with more than one leaf migration. Returns a dict of the app labels
        that conflict with the migration names that conflict.
        """
        seen_apps = {}
        conflicting_apps = set()
        for app_label, migration_name in self.graph.leaf_nodes():
            if app_label in seen_apps:
                conflicting_apps.add(app_label)
            seen_apps.setdefault(app_label, set()).add(migration_name)
        return dict((app_label, seen_apps[app_label]) for app_label in conflicting_apps)


class BadMigrationError(Exception):
    """
    Raised when there's a bad migration (unreadable/bad format/etc.)
    """
    pass


class AmbiguityError(Exception):
    """
    Raised when more than one migration matches a name prefix
    """
    pass
