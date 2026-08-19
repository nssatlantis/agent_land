"""Allow ``python -m viewer`` as a standalone entry point."""
from viewer import app, HOST, PORT

import logutil
import db

if __name__ == "__main__":
    import uvicorn
    logutil.configure_logging()
    db.init_db()
    print(db.database_location_note())
    logutil.log("viewer_startup", db=db.DB_PATH, host=HOST, port=PORT)
    uvicorn.run(app, host=HOST, port=PORT, log_level="warning")
