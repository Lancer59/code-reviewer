"""Run once to create the Chainlit SQLite schema."""
import sqlite3, os

os.makedirs("agent_data", exist_ok=True)
conn = sqlite3.connect("agent_data/chainlit_ui.db")
conn.executescript("""
CREATE TABLE IF NOT EXISTS users (
    "id" TEXT PRIMARY KEY, "identifier" TEXT NOT NULL UNIQUE,
    "metadata" TEXT NOT NULL, "createdAt" TEXT
);
CREATE TABLE IF NOT EXISTS threads (
    "id" TEXT PRIMARY KEY, "createdAt" TEXT, "name" TEXT,
    "userId" TEXT, "userIdentifier" TEXT, "tags" TEXT, "metadata" TEXT,
    FOREIGN KEY ("userId") REFERENCES users("id") ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS steps (
    "id" TEXT PRIMARY KEY, "name" TEXT NOT NULL, "type" TEXT NOT NULL,
    "threadId" TEXT NOT NULL, "parentId" TEXT, "streaming" INTEGER NOT NULL,
    "waitForAnswer" INTEGER, "isError" INTEGER, "metadata" TEXT, "tags" TEXT,
    "input" TEXT, "output" TEXT, "createdAt" TEXT, "command" TEXT,
    "start" TEXT, "end" TEXT, "generation" TEXT, "showInput" TEXT,
    "language" TEXT, "indent" INT, "defaultOpen" INTEGER, "autoCollapse" INTEGER,
    FOREIGN KEY ("threadId") REFERENCES threads("id") ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS elements (
    "id" TEXT PRIMARY KEY, "threadId" TEXT, "type" TEXT, "url" TEXT,
    "chainlitKey" TEXT, "name" TEXT NOT NULL, "display" TEXT, "objectKey" TEXT,
    "size" TEXT, "page" INT, "language" TEXT, "forId" TEXT, "mime" TEXT, "props" TEXT,
    FOREIGN KEY ("threadId") REFERENCES threads("id") ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS feedbacks (
    "id" TEXT PRIMARY KEY, "forId" TEXT NOT NULL, "threadId" TEXT NOT NULL,
    "value" INT NOT NULL, "comment" TEXT,
    FOREIGN KEY ("threadId") REFERENCES threads("id") ON DELETE CASCADE
);
""")
conn.commit()
conn.close()
print("DB schema created at agent_data/chainlit_ui.db")
