import sqlite3

# Conexión a la base de datos existente
conn = sqlite3.connect('database.db')

# Creación del cursor
cursor = conn.cursor()

# Agregar la tabla "peer"
cursor.execute('''
    CREATE TABLE IF NOT EXISTS peer (
        id TEXT PRIMARY KEY,
        token TEXT,
        metadata BLOB,
        app_protocol BLOB
    )
''')

# Agregar la tabla "Slot"
cursor.execute('''
    CREATE TABLE IF NOT EXISTS slot (
        id INTEGER PRIMARY KEY,
        internal_port INTEGER,
        transport_protocol BLOB,
        peer_id TEXT,
        FOREIGN KEY (peer_id) REFERENCES peer (id)
    )
''')

# Guardar los cambios y cerrar la conexión
conn.commit()
conn.close()
