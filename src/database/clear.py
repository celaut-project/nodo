import os

# Especifica el nombre del archivo de la base de datos
database_file = 'database.sqlite'

# Elimina el archivo de la base de datos
os.remove(database_file)

print("Database dropped.")
