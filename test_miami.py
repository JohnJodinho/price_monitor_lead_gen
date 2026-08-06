import asyncio
from db import AsyncSessionLocal
from sqlalchemy import text

async def main():
    async with AsyncSessionLocal() as session:
        result = await session.execute(text("SELECT id, name, market, latitude, longitude, url FROM properties WHERE market ILIKE '%Miami%' LIMIT 5"))
        for row in result.all():
            print(dict(row._mapping))

asyncio.run(main())
