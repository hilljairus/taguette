import alembic.command
import alembic.config
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory
import binascii
import contextlib
from datetime import datetime
import enum
import hashlib
import hmac
import json
import logging
import os
import prometheus_client
import shutil
from sqlalchemy import Column, ForeignKey, Index, TypeDecorator, MetaData, \
    UniqueConstraint, create_engine, select
import sqlalchemy.engine
import sqlalchemy.event
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import column_property, deferred, relationship, \
    sessionmaker
from sqlalchemy.sql import functions
from sqlalchemy.types import DateTime, Enum, Integer, String, Text
import sys

import taguette


logger = logging.getLogger(__name__)


PROM_DATABASE_VERSION = prometheus_client.Gauge('database_version',
                                                "Database version",
                                                ['version'])
PROM_COMMAND = prometheus_client.Counter('commands_total',
                                         "Number of commands",
                                         ['type'])


meta = MetaData(naming_convention={
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
})
Base = declarative_base(metadata=meta)


class JSON(TypeDecorator):
    """Platform-independent JSON type.
    """
    impl = Text

    def process_bind_param(self, value, dialect):
        return json.dumps(value, sort_keys=True)

    def process_result_value(self, value, dialect):
        return json.loads(value)


class User(Base):
    __tablename__ = 'users'

    login = Column(String(30), primary_key=True)
    created = Column(DateTime, nullable=False,
                     default=lambda: datetime.utcnow())
    hashed_password = Column(String(120), nullable=True)
    password_set_date = Column(DateTime, nullable=True)
    language = Column(String(10), nullable=True)
    email = Column(String(256), nullable=True, index=True, unique=True)
    email_sent = Column(DateTime, nullable=True)
    projects = relationship('Project', secondary='project_members')

    def set_password(self, password, method='pbkdf2'):
        if method == 'bcrypt':
            import bcrypt
            h = bcrypt.hashpw(password.encode('utf-8'),
                              bcrypt.gensalt())
            self.hashed_password = 'bcrypt:%s' % h.decode('utf-8')
        elif method == 'pbkdf2':
            ITERATIONS = 500000
            salt = os.urandom(16)
            h = hashlib.pbkdf2_hmac('sha256', password.encode('utf-8'),
                                    salt, ITERATIONS)
            self.hashed_password = 'pbkdf2:%s$%d$%s' % (
                binascii.hexlify(salt).decode('ascii'),
                ITERATIONS,
                binascii.hexlify(h).decode('ascii'),
            )
        else:
            raise ValueError("Unsupported encryption method %r" % method)
        self.password_set_date = datetime.utcnow()

    def check_password(self, password):
        if self.hashed_password is None:
            return False
        elif self.hashed_password.startswith('bcrypt:'):
            import bcrypt
            return bcrypt.checkpw(password.encode('utf-8'),
                                  self.hashed_password[7:].encode('utf-8'))
        elif self.hashed_password.startswith('pbkdf2:'):
            pw = self.hashed_password[7:]
            salt, iterations, hash_pw = pw.split('$', 2)
            salt = binascii.unhexlify(salt.encode('ascii'))
            iterations = int(iterations, 10)
            hash_pw = binascii.unhexlify(hash_pw.encode('ascii'))
            return hmac.compare_digest(
                hash_pw,
                hashlib.pbkdf2_hmac('sha256', password.encode('utf-8'),
                                    salt, iterations)
            )
        else:
            logger.warning("Password uses unknown encryption method")
            return False

    def __repr__(self):
        return '<%s.%s %r>' % (
            self.__class__.__module__,
            self.__class__.__name__,
            self.login,
        )


class Project(Base):
    __tablename__ = 'projects'
    __table_args__ = ({'sqlite_autoincrement': True},)

    id = Column(Integer, primary_key=True)
    name = Column(String(200), nullable=False)
    description = Column(Text, nullable=False)
    created = Column(DateTime, nullable=False,
                     default=lambda: datetime.utcnow())
    members = relationship('User', secondary='project_members')
    commands = relationship('Command', cascade='all,delete-orphan',
                            passive_deletes=True)
    documents = relationship('Document', cascade='all,delete-orphan',
                             passive_deletes=True)
    tags = relationship('Tag', cascade='all,delete-orphan', order_by='Tag.id',
                        passive_deletes=True)
    groups = relationship('Group', cascade='all,delete-orphan',
                          passive_deletes=True)

    def __repr__(self):
        return '<%s.%s %r %r>' % (
            self.__class__.__module__,
            self.__class__.__name__,
            self.id,
            self.name,
        )


class Privileges(enum.Enum):
    ADMIN = 1
    MANAGE_DOCS = 3
    TAG = 2
    READ = 0

    # Admin operations
    def can_edit_project_meta(self):
        return self == Privileges.ADMIN

    def can_delete_project(self):
        return self == Privileges.ADMIN

    def can_edit_members(self):
        return self == Privileges.ADMIN

    # Document operations
    def can_edit_document(self):
        return self in (Privileges.ADMIN, Privileges.MANAGE_DOCS)
    can_delete_document = can_edit_document
    can_add_document = can_edit_document

    # Tagging operations
    def can_update_tag(self):
        return self in (Privileges.ADMIN, Privileges.MANAGE_DOCS,
                        Privileges.TAG)
    can_add_tag = can_update_tag
    can_delete_tag = can_update_tag
    can_merge_tags = can_update_tag

    def can_add_highlight(self):
        return self in (Privileges.ADMIN, Privileges.MANAGE_DOCS,
                        Privileges.TAG)
    can_delete_highlight = can_add_highlight


class ProjectMember(Base):
    __tablename__ = 'project_members'

    project_id = Column(Integer, ForeignKey('projects.id', ondelete='CASCADE'),
                        primary_key=True, index=True)
    project = relationship('Project')
    user_login = Column(String(30),
                        ForeignKey('users.login',
                                   ondelete='CASCADE', onupdate='CASCADE'),
                        primary_key=True, index=True)
    user = relationship('User')
    privileges = Column(Enum(Privileges), nullable=False)

    def __repr__(self):
        return '<%s.%s project_id=%r user_login=%r>' % (
            self.__class__.__module__,
            self.__class__.__name__,
            self.project_id,
            self.user_login,
        )


class Document(Base):
    __tablename__ = 'documents'
    __table_args__ = ({'sqlite_autoincrement': True},)

    id = Column(Integer, primary_key=True)
    name = Column(String(200), nullable=False)
    description = Column(Text, nullable=False)
    filename = Column(String(200), nullable=True)
    created = Column(DateTime, nullable=False,
                     default=lambda: datetime.utcnow())
    project_id = Column(Integer, ForeignKey('projects.id', ondelete='CASCADE'),
                        nullable=False, index=True)
    project = relationship('Project', back_populates='documents')
    contents = deferred(Column(Text, nullable=False))
    group = relationship('Group', secondary='document_groups')
    highlights = relationship('Highlight', cascade='all,delete-orphan',
                              passive_deletes=True)

    def __repr__(self):
        return '<%s.%s %r %r project_id=%r>' % (
            self.__class__.__module__,
            self.__class__.__name__,
            self.id,
            self.name,
            self.project_id,
        )


class Command(Base):
    __tablename__ = 'commands'
    __table_args__ = ({'sqlite_autoincrement': True},)

    id = Column(Integer, primary_key=True)
    date = Column(DateTime, nullable=False,
                  default=datetime.utcnow(), index=True)
    user_login = Column(String(30),
                        ForeignKey('users.login', onupdate='CASCADE'),
                        nullable=False)
    user = relationship('User')
    project_id = Column(Integer, ForeignKey('projects.id', ondelete='CASCADE'),
                        nullable=False, index=True)
    project = relationship('Project')
    document_id = Column(Integer,
                         nullable=True)  # Not ForeignKey, document can go away
    payload = Column(JSON, nullable=False)

    tag_count_changes = None

    __table_args__ = (
        Index('idx_project_document', 'project_id', 'document_id'),
    ) + __table_args__

    def __init__(self, **kwargs):
        if 'payload' in kwargs:
            PROM_COMMAND.labels(kwargs['payload']['type']).inc()
        super(Command, self).__init__(**kwargs)

    TYPES = {'project_meta', 'document_add', 'document_delete',
             'highlight_add', 'highlight_delete', 'tag_add', 'tag_delete',
             'tag_merge', 'member_add', 'member_remove'}

    for n in TYPES:
        PROM_COMMAND.labels(n).inc(0)

    @classmethod
    def project_meta(cls, user_login, project_id, name, description):
        return cls(
            user_login=user_login,
            project_id=project_id,
            payload={'type': 'project_meta',  # keep in sync above
                     'project_name': name,
                     'description': description},
        )

    @classmethod
    def document_add(cls, user_login, document):
        return cls(
            user_login=user_login,
            project=document.project,
            document_id=document.id,
            payload={'type': 'document_add',  # keep in sync above
                     'document_name': document.name,
                     'description': document.description},
        )

    @classmethod
    def document_delete(cls, user_login, document):
        assert isinstance(document, Document)
        return cls(
            user_login=user_login,
            project_id=document.project_id,
            document_id=document.id,
            payload={'type': 'document_delete'},  # keep in sync above
        )

    @classmethod
    def highlight_add(cls, user_login, document, highlight, tags):
        assert isinstance(highlight.id, int)
        return cls(
            user_login=user_login,
            project_id=document.project_id,
            document_id=document.id,
            payload={'type': 'highlight_add',  # keep in sync above
                     'highlight_id': highlight.id,
                     'start_offset': highlight.start_offset,
                     'end_offset': highlight.end_offset,
                     'tags': tags},
        )

    @classmethod
    def highlight_delete(cls, user_login, document, highlight_id):
        assert isinstance(highlight_id, int)
        return cls(
            user_login=user_login,
            project_id=document.project_id,
            document_id=document.id,
            payload={'type': 'highlight_delete',  # keep in sync above
                     'highlight_id': highlight_id},
        )

    @classmethod
    def tag_add(cls, user_login, tag):
        assert isinstance(tag, Tag)
        return cls(
            user_login=user_login,
            project_id=tag.project_id,
            payload={'type': 'tag_add',  # keep in sync above
                     'tag_id': tag.id,
                     'tag_path': tag.path,
                     'description': tag.description},
        )

    @classmethod
    def tag_delete(cls, user_login, project_id, tag_id):
        assert isinstance(project_id, int)
        assert isinstance(tag_id, int)
        return cls(
            user_login=user_login,
            project_id=project_id,
            payload={'type': 'tag_delete',  # keep in sync above
                     'tag_id': tag_id},
        )

    @classmethod
    def tag_merge(cls, user_login, project_id, tag_src, tag_dest):
        assert isinstance(project_id, int)
        assert isinstance(tag_src, int)
        assert isinstance(tag_dest, int)
        return cls(
            user_login=user_login,
            project_id=project_id,
            payload={'type': 'tag_merge',  # keep in sync above
                     'src_tag_id': tag_src,
                     'dest_tag_id': tag_dest},
        )

    @classmethod
    def member_add(cls, user_login, project_id, member_login, privileges):
        assert isinstance(project_id, int)
        assert isinstance(privileges, Privileges)
        return cls(
            user_login=user_login,
            project_id=project_id,
            payload={'type': 'member_add',  # keep in sync above
                     'member': member_login,
                     'privileges': privileges.name}
        )

    @classmethod
    def member_remove(cls, user_login, project_id, member_login):
        assert isinstance(project_id, int)
        return cls(
            user_login=user_login,
            project_id=project_id,
            payload={'type': 'member_remove',  # keep in sync above
                     'member': member_login}
        )

    def __repr__(self):
        return (
            '<%s.%s %r %r user_login=%r project_id=%r document_id=%r type=%r>'
        ) % (
            self.__class__.__module__,
            self.__class__.__name__,
            self.id,
            self.date,
            self.user_login,
            self.project_id,
            self.document_id,
            self.payload.get('type'),
        )


Project.last_event = column_property(
    select(
        [Command.id]
    ).where(
        Command.project_id == Project.id
    ).order_by(
        Command.id.desc()
    ).limit(1)
    .as_scalar()
)


class Highlight(Base):
    __tablename__ = 'highlights'
    __table_args__ = ({'sqlite_autoincrement': True},)

    id = Column(Integer, primary_key=True)
    document_id = Column(Integer, ForeignKey('documents.id',
                                             ondelete='CASCADE'),
                         nullable=False, index=True)
    document = relationship('Document', back_populates='highlights')
    start_offset = Column(Integer, nullable=False)
    end_offset = Column(Integer, nullable=False)
    snippet = Column(Text, nullable=False)
    tags = relationship('Tag', secondary='highlight_tags')

    def __repr__(self):
        return '<%s.%s %r document_id=%r tags=[%s]>' % (
            self.__class__.__module__,
            self.__class__.__name__,
            self.id,
            self.document_id,
            ' '.join(sorted(t.id for t in self.tags)),
        )


class Group(Base):
    __tablename__ = 'groups'
    __table_args__ = ({'sqlite_autoincrement': True},)

    id = Column(Integer, primary_key=True)
    project_id = Column(Integer, ForeignKey('projects.id', ondelete='CASCADE'),
                        nullable=False, index=True)
    project = relationship('Project')

    path = Column(String(200), nullable=False, index=True)
    description = Column(Text, nullable=False)

    __table_args__ = (
        UniqueConstraint('project_id', 'path'),
    ) + __table_args__

    documents = relationship('Document', secondary='document_groups')

    def __repr__(self):
        return '<%s.%s %r %r project_id=%r>' % (
            self.__class__.__module__,
            self.__class__.__name__,
            self.id,
            self.path,
            self.project_id,
        )


class DocumentGroup(Base):
    __tablename__ = 'document_groups'

    document_id = Column(Integer, ForeignKey('documents.id',
                                             ondelete='CASCADE'),
                         primary_key=True)
    group_id = Column(Integer, ForeignKey('groups.id', ondelete='CASCADE'),
                      primary_key=True)

    def __repr__(self):
        return '<%s.%s document_id=%r group_id=%r>' % (
            self.__class__.__module__,
            self.__class__.__name__,
            self.document_id,
            self.group_id,
        )


class Tag(Base):
    __tablename__ = 'tags'
    __table_args__ = ({'sqlite_autoincrement': True},)

    id = Column(Integer, primary_key=True)
    project_id = Column(Integer, ForeignKey('projects.id', ondelete='CASCADE'),
                        nullable=False, index=True)
    project = relationship('Project')

    path = Column(String(200), nullable=False, index=True)
    description = Column(Text, nullable=False)

    __table_args__ = (
        UniqueConstraint('project_id', 'path'),
    ) + __table_args__

    highlights = relationship('Highlight', secondary='highlight_tags')

    def __repr__(self):
        return '<%s.%s %r %r project_id=%r>' % (
            self.__class__.__module__,
            self.__class__.__name__,
            self.id,
            self.path,
            self.project_id,
        )


class HighlightTag(Base):
    __tablename__ = 'highlight_tags'

    highlight_id = Column(Integer, ForeignKey('highlights.id',
                                              ondelete='CASCADE'),
                          primary_key=True)
    highlight = relationship('Highlight')
    tag_id = Column(Integer, ForeignKey('tags.id',
                                        ondelete='CASCADE'),
                    primary_key=True)
    tag = relationship('Tag')

    def __repr__(self):
        return '<%s.%s highlight_id=%r tag_id=%r>' % (
            self.__class__.__module__,
            self.__class__.__name__,
            self.highlight_id,
            self.tag_id,
        )


Tag.highlights_count = column_property(
    select(
        [functions.count(HighlightTag.highlight_id)],
    ).where(
        HighlightTag.tag_id == Tag.id,
    ).correlate_except(HighlightTag)
)


def set_sqlite_pragma(dbapi_connection, connection_record):
    if set_sqlite_pragma.enabled:
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()


set_sqlite_pragma.enabled = True


@contextlib.contextmanager
def no_sqlite_pragma_check():
    taguette.database.set_sqlite_pragma.enabled = False
    try:
        yield
    finally:
        taguette.database.set_sqlite_pragma.enabled = True


def connect(db_url):
    """Connect to the database using an environment variable.
    """
    logger.info("Connecting to SQL database %r", db_url)
    kwargs = {}
    if db_url.startswith('sqlite:'):
        kwargs['connect_args'] = {'check_same_thread': False}
    engine = create_engine(db_url, **kwargs)
    # logging.getLogger('sqlalchemy.engine').setLevel(logging.INFO)

    if db_url.startswith('sqlite:'):
        sqlalchemy.event.listen(
            engine,
            "connect",
            set_sqlite_pragma,
        )

    alembic_cfg = alembic.config.Config()
    alembic_cfg.set_main_option('script_location', 'taguette:migrations')
    alembic_cfg.set_main_option('sqlalchemy.url', db_url)

    conn = engine.connect()
    if not engine.dialect.has_table(conn, Project.__tablename__):
        logger.warning("The tables don't seem to exist; creating")
        Base.metadata.create_all(bind=engine)

        # Mark this as the most recent Alembic version
        alembic.command.stamp(alembic_cfg, "head")

        # Set SQLite's "application ID"
        if db_url.startswith('sqlite:'):
            conn.execute("PRAGMA application_id=0x54677474;")  # 'Tgtt'
    else:
        # Perform Alembic migrations if needed
        context = MigrationContext.configure(conn)
        current_rev = context.get_current_revision()
        scripts = ScriptDirectory.from_config(alembic_cfg)
        if [current_rev] != scripts.get_heads():
            logger.warning("Database schema is out of date: %s", current_rev)
            _ = taguette.trans.gettext
            if db_url.startswith('sqlite:'):
                print(_("\n    The database schema used by Taguette has "
                        "changed! We will try to\n    update your workspace "
                        "automatically.\n"), file=sys.stderr, flush=True)
                assert db_url.startswith('sqlite:///')
                assert os.path.exists(db_url[10:])
                backup = db_url[10:] + '.bak'
                shutil.copy2(db_url[10:], backup)
                logger.warning("Performing automated update, backup file: %s",
                               backup)
                print(_("\n    A backup copy of your database file has been "
                        "created. If the update\n    goes horribly wrong, "
                        "make sure to keep that file, and let us know:\n    "
                        "%(backup)s\n") % dict(backup=backup),
                      file=sys.stderr, flush=True)
                alembic.command.upgrade(alembic_cfg, 'head')
            else:
                print(_("\n    The database schema used by Taguette has "
                        "changed! Because you are not using\n    SQLite, we "
                        "will not attempt a migration automatically; back up "
                        "your data and\n    use `taguette --database=%(url)s "
                        "migrate` if you want to proceed.") % dict(url=db_url),
                      file=sys.stderr, flush=True)
                sys.exit(3)
        else:
            logger.info("Database is up to date: %s", current_rev)

    # Record to Prometheus
    conn.close()
    conn = engine.connect()
    revision = MigrationContext.configure(conn).get_current_revision()
    PROM_DATABASE_VERSION.labels(revision).set(1)

    DBSession = sessionmaker(bind=engine)

    return DBSession


def migrate(db_url, revision):
    alembic_cfg = alembic.config.Config()
    alembic_cfg.set_main_option('script_location', 'taguette:migrations')
    alembic_cfg.set_main_option('sqlalchemy.url', db_url)

    logger.warning("Performing database upgrade")
    alembic.command.upgrade(alembic_cfg, revision)


def copy_table(src_db, dest_db, table,
               pkey, fkeys,
               *, batch_size=50, condition=None, transform=None):
    """Copy all data in a table across database connections.

    :param src_db: The SQLAlchemy ``Connection`` or ``Session`` to copy from.
    :param dest_db: The SQLAlchemy ``Connection`` or ``Session`` to copy into.
    :param table: The SQLAlchemy ``Table`` we are copying. It should exist on
        both the source and destination databases.
    :param pkey: The field in the table that is the primary key and should be
        reset during the copy (so new values get generated when inserting in
        the destination and no conflict occurs).
    :param fkeys: A dictionary associating the name of the fields that are
        foreign keys to a dictionary mapping the keys.
    :param transform: Function to apply on each row.
    """
    query = table.select()
    if pkey is not None:
        query = query.order_by(pkey)
    if condition is not None:
        query = query.where(condition)
    query = src_db.execute(query)
    assert pkey is None or pkey in query.keys()
    assert all(field in query.keys() for field in fkeys)
    mapping = {}
    batch = query.fetchmany(batch_size)
    orig_pkey = None  # Avoids warning
    while batch:
        for row in batch:
            row = dict(row.items())
            # Get primary key, reset it to None
            if pkey is not None:
                orig_pkey = row[pkey]
                row[pkey] = None
            # Map foreign keys
            for field, fkey_map in fkeys.items():
                row[field] = fkey_map[row[field]]
            # Generic transform
            if transform is not None:
                row = transform(row)
            # Have to insert one-by-one for inserted_primary_key
            ins = dest_db.execute(
                table.insert(),
                row,
            )
            # Store new primary key
            if pkey is not None:
                mapping[orig_pkey], = ins.inserted_primary_key
        batch = query.fetchmany(batch_size)
    return mapping
