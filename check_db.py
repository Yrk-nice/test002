import sqlite3
import os

db_path = os.path.join('app', 'app.db')
if not os.path.exists(db_path):
    print("Database file not found!")
else:
    try:
        conn = sqlite3.connect(db_path)
        cursor = conn.cursor()
        cursor.execute("SELECT name FROM sqlite_master WHERE type='table';")
        tables = cursor.fetchall()
        print("Tables:", [t[0] for t in tables])
        
        if 'menus' in [t[0] for t in tables]:
            cursor.execute("PRAGMA table_info(menus)")
            columns = cursor.fetchall()
            print("Menus columns:", [c[1] for c in columns])
            
            cursor.execute("SELECT * FROM menus")
            rows = cursor.fetchall()
            print("Menus count:", len(rows))
        else:
            print("Table 'menus' does NOT exist!")
            
        conn.close()
    except Exception as e:
        print("Error:", e)
