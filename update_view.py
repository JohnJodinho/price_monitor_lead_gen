import asyncio
from sqlalchemy import text
from db import AsyncSessionLocal

async def update_view():
    with open("../misc/new_real_estate_views.sql", "r") as f:
        sql = f.read()
    async with AsyncSessionLocal() as session:
        await session.execute(text(sql))
        await session.commit()
    print("View updated successfully.")

asyncio.run(update_view())
