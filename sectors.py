"""
sectors.py — one sector tag per ticker.

Boardroom #2 ruling (2026-09-01), item 7: crypto / digital-asset-treasury
names are NOT excluded and trade at standard risk. The condition attached to
that ruling is that the class must be MEASURABLE — so every position and
every trade carries a sector tag and the class's live expectancy is reported
next to every other sector's. If the class turns out to be a loser, the
evidence will say so; the exclusion argument does not get to run on vibes.

"crypto_dat" covers three things that behave as one class here: miners,
exchanges/brokers whose revenue is crypto beta, and treasury companies whose
market cap is a leveraged claim on a coin (the BMNR/MSTR shape).
"""

# Names whose price is, in practice, levered crypto beta.
CRYPTO_DAT = {
    # treasury companies (equity as a leveraged coin claim)
    "MSTR", "BMNR", "SBET", "DFDV", "UPXI", "SMLR", "NAKA", "BTCS", "ETHZ",
    # miners
    "MARA", "RIOT", "CLSK", "HUT", "BITF", "CIFR", "WULF", "IREN", "CORZ",
    "HIVE", "BTDR", "GREE", "SDIG",
    # exchanges / brokers whose revenue is crypto beta
    "COIN", "BKKT", "GLXY",
}

SECTOR_MAP = {}


def _add(sector, symbols):
    for s in symbols:
        SECTOR_MAP[s] = sector


_add("technology", [
    "AAPL", "MSFT", "GOOGL", "AMZN", "META", "NVDA", "AVGO", "ORCL", "CRM",
    "ADBE", "AMD", "INTC", "QCOM", "TXN", "MU", "AMAT", "LRCX", "KLAC", "ADI",
    "NXPI", "MRVL", "ON", "SWKS", "MCHP", "CSCO", "IBM", "ACN", "NOW", "INTU",
    "PANW", "CRWD", "SNOW", "DDOG", "NET", "ZS", "WDAY", "SHOP", "SQ", "PYPL",
    "PLTR", "TTD", "DELL", "HPQ", "SMCI", "ANET", "FTNT", "APP", "ARM", "U",
    "TEAM", "MDB", "OKTA", "HUBS", "CDNS", "SNPS", "TER", "ENTG", "WDC", "STX",
    "HPE", "NOK", "ERIC",
])
_add("communication", [
    "NFLX", "SNAP", "PINS", "RBLX", "ROKU", "SPOT", "EA", "TTWO", "DIS", "WBD",
    "CMCSA", "T", "VZ", "TMUS", "GOOG", "PARA",
])
_add("consumer_discretionary", [
    "TSLA", "UBER", "LYFT", "ABNB", "DASH", "HD", "LOW", "NKE", "SBUX", "MCD",
    "CMG", "DKNG", "LULU", "TJX", "CCL", "RCL", "MAR", "BKNG", "F", "GM",
    "RIVN", "LCID", "TGT", "YUM", "ROST", "DG", "DLTR", "NCLH", "HLT", "EXPE",
    "TSCO",
])
_add("consumer_staples", [
    "WMT", "COST", "PEP", "KO", "PG", "MO", "PM", "STZ", "KR", "MDLZ", "CL",
    "KMB", "GIS", "TAP",
])
_add("financials", [
    "JPM", "BAC", "WFC", "C", "GS", "MS", "SCHW", "BLK", "AXP", "COF", "USB",
    "PNC", "TFC", "V", "MA", "SOFI", "HOOD", "ALLY", "BRK.B", "PGR", "KEY",
    "RF", "CFG", "AIG", "MET", "PRU",
])
_add("healthcare", [
    "UNH", "JNJ", "LLY", "PFE", "MRK", "ABBV", "BMY", "AMGN", "GILD", "VRTX",
    "REGN", "MRNA", "CVS", "CI", "HUM", "ISRG", "MDT", "TMO", "DHR", "ABT",
    "BSX", "SYK", "HIMS", "BIIB", "VTRS", "TEVA", "ZTS",
])
_add("industrials", [
    "BA", "CAT", "DE", "GE", "HON", "MMM", "LMT", "RTX", "NOC", "UPS", "FDX",
    "UNP", "CSX", "DAL", "UAL", "AAL", "GD", "NSC", "LUV", "SPCX", "RKLB",
    "ASTS", "LUNR",
])
_add("energy", [
    "XOM", "CVX", "COP", "OXY", "SLB", "DVN", "FANG", "MPC", "PSX", "VLO",
    "KMI", "WMB", "OKE", "HAL", "APA", "MRO",
])
_add("materials", [
    "FCX", "NEM", "CLF", "X", "NUE", "AA", "LIN", "MOS", "CF", "DOW", "SHW",
    "VMC",
])
_add("utilities_reit", [
    "NEE", "DUK", "SO", "AEP", "PLD", "AMT", "EXC", "SRE", "CCI", "SPG", "O",
])
_add("crypto_dat", sorted(CRYPTO_DAT))

UNCLASSIFIED = "unclassified"

_NAME_HINTS = (
    ("BITCOIN", "crypto_dat"), ("ETHEREUM", "crypto_dat"),
    ("BLOCKCHAIN", "crypto_dat"), ("DIGITAL ASSET", "crypto_dat"),
    ("MINING", "crypto_dat"), ("CRYPTO", "crypto_dat"),
)


def sector_for(ticker: str, name: str = "") -> str:
    """Sector tag for a ticker. `name` (the asset's long name, when we have
    it) is a fallback so a DAT microcap that isn't on the list yet still gets
    classified rather than silently landing in 'unclassified'."""
    sym = str(ticker or "").upper().strip()
    if not sym:
        return UNCLASSIFIED
    if sym in SECTOR_MAP:
        return SECTOR_MAP[sym]
    upper = str(name or "").upper()
    for hint, sector in _NAME_HINTS:
        if hint in upper:
            return sector
    return UNCLASSIFIED


def is_crypto_dat(ticker: str, name: str = "") -> bool:
    return sector_for(ticker, name) == "crypto_dat"
