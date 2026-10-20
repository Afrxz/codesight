"""Market data ingestion — the one deliberate abstraction in this project.

`MarketSource` (see base.py) defines the contract; concrete sources
(`MockSource`, `TcgApiSource`) implement it. The rest of the system depends only
on the normalized `MarketData` shape (see types.py), never on a specific source.
"""
