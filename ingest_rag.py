"""
STM32 Motor Control SDK – RAG Ingestion Pipeline
=================================================
Parses HTML documentation, chunks content, classifies engineering topics,
generates embeddings locally via sentence-transformers (BAAI/bge-small-en)
and upserts into Qdrant.

Dependencies:
    pip install beautifulsoup4 lxml qdrant-client sentence-transformers

Usage:
    python ingest_rag.py
"""

from __future__ import annotations

import hashlib
import logging
import re
import time
import uuid
from pathlib import Path
from typing import Any

from sentence_transformers import SentenceTransformer
from bs4 import BeautifulSoup, Tag
from qdrant_client import QdrantClient
from qdrant_client.http.models import (
    Distance,
    PointStruct,
    VectorParams,
)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

HTML_DIR = Path(r"C:\Program Files (x86)\STMicroelectronics\MC_SDK_6.4.1\Documentation\html")

QDRANT_URL = "http://192.168.1.115:6333"
COLLECTION_NAME = "motor_control_rag"

EMBEDDING_MODEL = "BAAI/bge-small-en"  # fallback: all-MiniLM-L6-v2
VECTOR_DIM = 384   # BAAI/bge-small-en output dimension

CHUNK_MIN_WORDS = 60        # absolute floor – skip smaller pieces
CHUNK_TARGET_WORDS = 400    # soft target per chunk
CHUNK_MAX_WORDS = 700       # hard ceiling before forced split
OVERLAP_WORDS = 60          # words carried from previous chunk

BATCH_SIZE = 50             # Qdrant upsert batch
EMBED_BATCH_SIZE = 64       # sentence-transformers encode batch size

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Sentence-Transformers model (loaded once at startup)
# ---------------------------------------------------------------------------

log.info("Loading embedding model '%s' …", EMBEDDING_MODEL)
_st_model = SentenceTransformer(EMBEDDING_MODEL)
log.info("Embedding model loaded – vector dim=%d", VECTOR_DIM)

# ---------------------------------------------------------------------------
# Topic / Type classification tables
# ---------------------------------------------------------------------------

TOPIC_KEYWORDS: dict[str, list[str]] = {
    "PMSM": [
        "pmsm", "permanent magnet", "synchronous motor", "rotor", "stator",
        "back emf", "bemf", "flux linkage", "pole pair", "reluctance",
        "magnetic", "motor structure", "saliency", "cogging", "torque ripple",
        "motor parameters", "rs", "ld", "lq", "pmsm_motor_parameters",
    ],
    "FOC": [
        "field oriented", "foc", "dq", "d-axis", "q-axis", "clark",
        "clarke", "park", "inverse park", "space vector", "svpwm",
        "vector control", "iq", "id", "vd", "vq", "alpha beta",
        "reference frame", "dq transform", "current regulator",
        "foc algorithm", "focvars", "current control",
    ],
    "STM32": [
        "stm32", "hal", "cubemx", "tim", "adc", "dma", "irq", "nvic",
        "pwm timer", "gpio", "rcc", "stm32g4", "stm32f4", "stm32l4",
        "stm32h7", "microcontroller", "mcu", "cpu load", "freertos",
        "rtos", "cortex", "arm", "flash", "ram", "register",
    ],
    "WORKBENCH": [
        "motor control workbench", "workbench", "mc workbench", "wizard",
        "motor pilot", "gui", "toolbar", "project generation", "stm32cubemx",
        "mc sdk", "mcsdk", "motor profiler", "one touch tuning", "ott",
        "motorcontrol workbench",
    ],
    "CONTROL_LOOP": [
        "pid", "pi regulator", "speed loop", "current loop", "torque loop",
        "kp", "ki", "integral", "proportional", "bandwidth", "gain",
        "speed controller", "current controller", "closed loop", "open loop",
        "feed forward", "anti windup", "saturation", "ramp", "step response",
        "flux weakening", "mtpa", "maximum torque", "circle limitation",
        "speed torque", "position control", "trajectory",
    ],
    "PARAMETER": [
        "parameter", "configuration", "define", "macro", "threshold",
        "drive_parameters", "parameters_conversion", "mc_parameters",
        "motor_parameters", "power_stage", "register", "tuning parameter",
        "default value", "scaling factor", "measurement unit", "s16",
    ],
}

TYPE_KEYWORDS: dict[str, list[str]] = {
    "api": [
        "function", "api", "prototype", "return", "parameter description",
        "mc_", "mcapi", "handle", "brief", "struct", "typedef", "enum",
        "mc_interface", "mc_api",
    ],
    "tuning": [
        "tuning", "kp", "ki", "gain setting", "bandwidth", "oscillation",
        "overshoot", "settling", "one touch tuning", "ott", "pid gains",
        "speed pi", "current pi", "flux pi", "auto tuning",
    ],
    "configuration": [
        "configuration", "setup", "workbench", "wizard", "cubemx",
        "drive_parameters", "mc_parameters", "parameters_conversion",
        "flash", "non volatile", "register setting", "#define",
    ],
    "implementation": [
        "implementation", "algorithm", "state machine", "interrupt",
        "isr", "callback", "task", "rtos", "firmware", "code",
        "mc_tasks", "six step", "foc algo",
    ],
    "theory": [
        "equation", "formula", "theory", "principle", "derivation",
        "mathematical", "model", "physics", "fundamental", "background",
        "motor model", "space vector theory", "park transform theory",
    ],
}

COMPLEXITY_MARKERS = {
    "advanced": [
        "mtpa", "flux weakening", "observers", "bemf observer", "sto pll",
        "cordic", "sensorless", "hso", "overmodulation", "discontinuous pwm",
        "single shunt", "profiler", "self commissioning",
    ],
    "intermediate": [
        "foc", "dq", "park", "clarke", "svpwm", "pid tuning", "feed forward",
        "encoder", "hall sensor", "current sensing", "speed loop", "current loop",
        "circle limitation", "ramp manager",
    ],
}

ENGINEERING_PROBLEMS = [
    (["speed oscillation", "speed ripple", "hunting"], "speed oscillation or instability",
     "when motor speed fluctuates or oscillates near setpoint",
     ["speed_kp", "speed_ki", "speed_bandwidth"]),
    (["overcurrent", "over current", "current limit", "current protection"],
     "overcurrent fault or current limiting",
     "when motor draws excessive current or triggers OCP",
     ["current_kp", "current_ki", "nominal_current", "peak_current"]),
    (["startup failure", "start-up failure", "rev up", "alignment"], "startup failure or alignment error",
     "during motor startup phase or initial alignment",
     ["revup_phase", "alignment_angle", "startup_current"]),
    (["torque ripple", "cogging", "ripple"], "torque ripple and cogging reduction",
     "when mechanical vibration or torque non-uniformity is observed",
     ["Iq", "Id", "harmonic_compensation"]),
    (["flux weakening", "field weakening", "high speed"], "operation above base speed (flux weakening)",
     "when motor must operate beyond rated speed",
     ["flux_weakening_kp", "flux_weakening_ki", "Vdcbus", "Id_reference"]),
    (["sensorless", "bemf", "observer", "sto"], "sensorless rotor position estimation",
     "when encoder/hall sensors are not used and BEMF observer controls position",
     ["sto_kp", "sto_ki", "bemf_gain", "pll_kp", "pll_ki"]),
    (["pid", "pi regulator", "kp", "ki", "gain"], "PID/PI controller gain tuning",
     "when controller gains need adjustment for stability and response",
     ["Kp", "Ki", "Kd", "integral_limit"]),
    (["position control", "trajectory", "position loop"], "position control and trajectory following",
     "when precise angular position control is required",
     ["position_kp", "position_bandwidth", "trajectory_jerk"]),
    (["temperature", "thermal", "overheating", "ntc"], "thermal protection and monitoring",
     "when motor or drive overheating is detected or managed",
     ["temperature_threshold", "ntc_params"]),
    (["dc bus", "bus voltage", "overvoltage", "undervoltage"], "DC bus voltage fault",
     "when bus voltage exceeds or falls below safe operating limits",
     ["vbus_overvoltage", "vbus_undervoltage", "vbus_nominal"]),
]

RELATED_PARAMS_GLOBAL = [
    "Kp", "Ki", "Kd", "Iq", "Id", "Iq_ref", "Id_ref",
    "speed_kp", "speed_ki", "current_kp", "current_ki",
    "Vdcbus", "nominal_current", "peak_current", "R_phase",
    "L_phase", "Ld", "Lq", "Rs", "flux_linkage", "pole_pairs",
    "max_speed", "min_speed", "nominal_speed", "pwm_frequency",
    "revup_duration", "revup_final_speed",
]

# ---------------------------------------------------------------------------
# HTML Parsing
# ---------------------------------------------------------------------------

SKIP_TAGS = {"script", "style", "nav", "footer", "header", "noscript", "iframe"}
SKIP_CLASS_PATTERNS = re.compile(
    r"navpath|navrow|memdoc|memitem|ttc|field|ftvtree|levels|tabs|"
    r"directory|dirtab|icona|arrow|search|memtitle",
    re.IGNORECASE,
)


def _tag_is_noise(tag: Tag) -> bool:
    """Return True for navigation / chrome elements that add no content."""
    if tag.name in SKIP_TAGS:
        return True
    cls = " ".join(tag.get("class", []))
    if SKIP_CLASS_PATTERNS.search(cls):
        return True
    return False


def _extract_text_blocks(soup: BeautifulSoup) -> list[tuple[str, str]]:
    """
    Walk the parsed HTML and return a list of (heading, body_text) pairs.
    Each pair represents a logical section of the document.
    """
    sections: list[tuple[str, str]] = []
    current_heading = ""
    current_lines: list[str] = []

    def flush() -> None:
        text = " ".join(current_lines).strip()
        if text:
            sections.append((current_heading, text))

    body = soup.find("body") or soup
    for elem in body.descendants:
        if not isinstance(elem, Tag):
            continue
        if _tag_is_noise(elem):
            continue
        if elem.name in {"h1", "h2", "h3"}:
            flush()
            current_heading = elem.get_text(" ", strip=True)
            current_lines = []
        elif elem.name in {"p", "li", "dt", "dd"}:
            txt = elem.get_text(" ", strip=True)
            if txt:
                current_lines.append(txt)
        elif elem.name in {"pre", "code"}:
            code = elem.get_text("\n", strip=True)
            if code and len(code.split()) <= 120:
                current_lines.append(f"[CODE] {code}")
        elif elem.name == "table":
            for row in elem.find_all("tr"):
                cells = [c.get_text(" ", strip=True) for c in row.find_all(["td", "th"])]
                row_text = " | ".join(c for c in cells if c)
                if row_text:
                    current_lines.append(row_text)

    flush()
    return sections


def parse_html_file(path: Path) -> dict[str, Any]:
    """Parse a single HTML file and return structured content."""
    try:
        raw = path.read_bytes()
        soup = BeautifulSoup(raw, "lxml")
    except Exception as exc:
        log.warning("Could not parse %s: %s", path.name, exc)
        return {}

    title_tag = soup.find("title")
    doc_title = title_tag.get_text(strip=True) if title_tag else ""
    if not doc_title:
        h1 = soup.find("h1")
        doc_title = h1.get_text(strip=True) if h1 else path.stem

    sections = _extract_text_blocks(soup)
    return {
        "document_title": doc_title,
        "source_file": str(path),
        "file_name": path.name,
        "sections": sections,
    }


# ---------------------------------------------------------------------------
# Chunking
# ---------------------------------------------------------------------------

def chunk_section(heading: str, text: str) -> list[str]:
    """
    Split a section into overlapping word-count-bounded chunks.
    """
    words = text.split()
    if len(words) < CHUNK_MIN_WORDS:
        return [text] if words else []

    chunks: list[str] = []
    start = 0
    while start < len(words):
        end = min(start + CHUNK_TARGET_WORDS, len(words))
        if len(words) - end < CHUNK_MIN_WORDS:
            end = len(words)
        chunk_words = words[start:end]
        chunk = " ".join(chunk_words)
        if heading:
            chunk = f"{heading}: {chunk}"
        chunks.append(chunk)
        if end >= len(words):
            break
        start = end - OVERLAP_WORDS
    return chunks


def build_chunks(parsed: dict[str, Any]) -> list[dict[str, Any]]:
    """Convert parsed document sections into annotated chunk records."""
    chunks_out: list[dict[str, Any]] = []
    for heading, text in parsed.get("sections", []):
        for chunk_text in chunk_section(heading, text):
            chunks_out.append({
                "content": chunk_text,
                "section_title": heading,
                "document_title": parsed["document_title"],
                "source_file": parsed["source_file"],
                "file_name": parsed["file_name"],
            })
    return chunks_out


# ---------------------------------------------------------------------------
# Classification
# ---------------------------------------------------------------------------

def _score_keywords(text_lower: str, kw_list: list[str]) -> int:
    return sum(1 for kw in kw_list if kw in text_lower)


def classify_topic(text: str, file_name: str) -> str:
    combined = text.lower() + " " + file_name.lower()
    scores = {topic: _score_keywords(combined, kws) for topic, kws in TOPIC_KEYWORDS.items()}
    best = max(scores, key=lambda k: scores[k])
    return best if scores[best] > 0 else "STM32"


def classify_type(text: str, file_name: str) -> str:
    combined = text.lower() + " " + file_name.lower()
    scores = {t: _score_keywords(combined, kws) for t, kws in TYPE_KEYWORDS.items()}
    best = max(scores, key=lambda k: scores[k])
    return best if scores[best] > 0 else "implementation"


def classify_complexity(text: str) -> str:
    tl = text.lower()
    for level, markers in COMPLEXITY_MARKERS.items():
        if any(m in tl for m in markers):
            return level
    return "basic"


def derive_subtopic(text: str, topic: str) -> str:
    tl = text.lower()
    subtopic_map: dict[str, list[tuple[str, str]]] = {
        "PMSM":        [("bemf", "BEMF_estimation"), ("torque ripple", "torque_ripple"),
                        ("flux linkage", "flux_linkage"), ("motor parameters", "motor_parameters"),
                        ("saliency", "saliency"), ("pole pair", "pole_pairs")],
        "FOC":         [("flux weakening", "flux_weakening"), ("mtpa", "MTPA"),
                        ("svpwm", "SVPWM"), ("observer", "observer"),
                        ("clark", "Clarke_Park"), ("feed forward", "feed_forward"),
                        ("circle limit", "circle_limitation")],
        "STM32":       [("adc", "ADC"), ("tim", "timer"), ("dma", "DMA"),
                        ("freertos", "FreeRTOS"), ("interrupt", "interrupt"),
                        ("current sensing", "current_sensing")],
        "WORKBENCH":   [("motor profiler", "motor_profiler"), ("one touch", "OTT"),
                        ("wizard", "wizard"), ("motor pilot", "motor_pilot")],
        "CONTROL_LOOP":[("speed", "speed_controller"), ("current", "current_controller"),
                        ("position", "position_controller"), ("ramp", "ramp_manager"),
                        ("feed forward", "feed_forward")],
        "PARAMETER":   [("drive_param", "drive_parameters"), ("conversion", "parameter_conversion"),
                        ("mc_param", "mc_parameters"), ("power_stage", "power_stage_parameters")],
    }
    for keyword, label in subtopic_map.get(topic, []):
        if keyword in tl:
            return label
    return topic.lower()


def extract_keywords(text: str) -> list[str]:
    """Extract domain-relevant keywords from chunk text."""
    tl = text.lower()
    vocabulary = [
        "kp", "ki", "kd", "pid", "pi", "iq", "id", "vd", "vq",
        "bemf", "foc", "pmsm", "stm32", "svpwm", "adc", "pwm",
        "speed", "torque", "current", "voltage", "flux", "rotor",
        "stator", "encoder", "hall", "sensorless", "observer",
        "sto", "pll", "cordic", "dq", "clark", "park",
        "flux weakening", "mtpa", "circle limitation", "feed forward",
        "anti windup", "ramp", "revup", "startup",
        "overvoltage", "overcurrent", "temperature", "ntc",
        "rs", "ld", "lq", "bandwidth", "pole pair",
        "dc bus", "bus voltage", "switching frequency", "dead time",
        "sampling", "oversampling", "single shunt", "three shunt",
        "gain", "integral", "proportional",
    ]
    found = [kw for kw in vocabulary if kw in tl]
    param_re = re.compile(r"\b([A-Z][a-z]?[A-Za-z0-9_]{1,20})\b")
    for match in param_re.finditer(text):
        token = match.group(1)
        if token not in found and len(token) <= 24:
            found.append(token)
    seen: set[str] = set()
    result: list[str] = []
    for kw in found:
        if kw not in seen:
            seen.add(kw)
            result.append(kw)
        if len(result) == 15:
            break
    return result


def infer_engineering_context(text: str) -> dict[str, Any]:
    """Rule-based inference of engineering problem, scenario, and related params."""
    tl = text.lower()
    for triggers, problem, when_to_use, params in ENGINEERING_PROBLEMS:
        if any(t in tl for t in triggers):
            extra = [p for p in RELATED_PARAMS_GLOBAL if p.lower() in tl and p not in params]
            return {
                "problem": problem,
                "when_to_use": when_to_use,
                "related_parameters": params + extra[:4],
            }
    found_params = [p for p in RELATED_PARAMS_GLOBAL if p.lower() in tl][:6]
    return {
        "problem": "general motor control configuration or operation",
        "when_to_use": "during system setup or debugging motor behavior",
        "related_parameters": found_params or ["Kp", "Ki"],
    }


def annotate_chunk(chunk: dict[str, Any], idx: int, total: int) -> dict[str, Any]:
    """Fill in all classification and metadata fields for a chunk."""
    text = chunk["content"]
    file_name = chunk["file_name"]
    topic = classify_topic(text, file_name)
    doc_type = classify_type(text, file_name)
    complexity = classify_complexity(text)
    subtopic = derive_subtopic(text, topic)
    keywords = extract_keywords(text)
    eng_ctx = infer_engineering_context(text)

    uid_seed = f"{chunk['source_file']}::{idx}"
    point_id = str(uuid.UUID(hashlib.md5(uid_seed.encode()).hexdigest()))

    return {
        "id": point_id,
        "content": text,
        "section_title": chunk["section_title"],
        "document_title": chunk["document_title"],
        "source_file": chunk["source_file"],
        "topic": topic,
        "subtopic": subtopic,
        "type": doc_type,
        "keywords": keywords,
        "complexity": complexity,
        "chunk_index": idx,
        "total_chunks": total,
        "embedding_model": EMBEDDING_MODEL,
        "engineering_context": eng_ctx,
    }


# ---------------------------------------------------------------------------
# Embedding
# ---------------------------------------------------------------------------

def embedding_function(texts: str | list[str]) -> list[list[float]]:
    """
    Generate embeddings using sentence-transformers with batch processing.

    Fully local – no network calls. Uses cosine-compatible normalised vectors.

    Args:
        texts: a single string or a list of strings.

    Returns:
        list[list[float]] – one 384-d vector per non-empty input text.
    """
    if isinstance(texts, str):
        texts = [texts]

    # Normalise and drop empties
    cleaned = [t.replace("\n", " ").strip() for t in texts]
    cleaned = [t for t in cleaned if t]

    if not cleaned:
        return []

    vecs = _st_model.encode(
        cleaned,
        batch_size=EMBED_BATCH_SIZE,
        show_progress_bar=False,
        normalize_embeddings=True,          # critical for cosine similarity in Qdrant
    )
    return vecs.tolist()


def embed_chunks(annotated: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """
    Attach embedding vectors to all annotated chunks using true batch encoding.

    Processes all texts in one pass through sentence-transformers, then
    maps vectors back to their source chunks.  Skips chunks with blank content.
    """
    total = len(annotated)
    log.info("Embedding %d chunks in batches of %d …", total, EMBED_BATCH_SIZE)

    # Build a parallel list of (original_index, clean_text) for non-empty chunks
    valid: list[tuple[int, str]] = []
    for i, chunk in enumerate(annotated):
        text = chunk["content"].replace("\n", " ").strip()
        if text:
            valid.append((i, text))
        else:
            log.warning("Skipping chunk %d – empty content", i)

    if not valid:
        log.error("No valid chunks to embed.")
        return []

    indices, texts = zip(*valid)

    # Single batched encode call – avoids per-text overhead on CPU
    log.info("  Running SentenceTransformer.encode on %d texts …", len(texts))
    t0 = time.perf_counter()
    vectors = _st_model.encode(
        list(texts),
        batch_size=EMBED_BATCH_SIZE,
        show_progress_bar=True,
        normalize_embeddings=True,
    )
    elapsed = time.perf_counter() - t0
    log.info("  Encoding complete in %.1fs (%.0f chunks/s)",
             elapsed, len(texts) / max(elapsed, 1e-6))

    result: list[dict[str, Any]] = []
    for orig_idx, vec in zip(indices, vectors):
        annotated[orig_idx]["vector"] = vec.tolist()
        result.append(annotated[orig_idx])

    log.info("Embedded %d/%d chunks successfully", len(result), total)
    return result


# ---------------------------------------------------------------------------
# Qdrant
# ---------------------------------------------------------------------------

def get_qdrant_client() -> QdrantClient:
    return QdrantClient(url=QDRANT_URL, timeout=30)


def ensure_collection(client: QdrantClient) -> None:
    collections = {c.name: c for c in client.get_collections().collections}
    if COLLECTION_NAME in collections:
        # Check for dimension mismatch and recreate if needed
        info = client.get_collection(COLLECTION_NAME)
        existing_dim = info.config.params.vectors.size
        if existing_dim != VECTOR_DIM:
            log.warning(
                "Collection '%s' has dim=%d but model requires dim=%d – recreating.",
                COLLECTION_NAME, existing_dim, VECTOR_DIM,
            )
            client.delete_collection(COLLECTION_NAME)
        else:
            log.info("Collection '%s' already exists with correct dim=%d – will upsert", COLLECTION_NAME, VECTOR_DIM)
            return
    log.info("Creating collection '%s' with dim=%d", COLLECTION_NAME, VECTOR_DIM)
    client.create_collection(
        collection_name=COLLECTION_NAME,
        vectors_config=VectorParams(size=VECTOR_DIM, distance=Distance.COSINE),
    )


def upsert_points(client: QdrantClient, embedded: list[dict[str, Any]]) -> None:
    total = len(embedded)
    log.info("Upserting %d points in batches of %d …", total, BATCH_SIZE)
    for batch_start in range(0, total, BATCH_SIZE):
        batch = embedded[batch_start : batch_start + BATCH_SIZE]
        points = [
            PointStruct(
                id=c["id"],
                vector=c["vector"],
                payload={
                    "content": c["content"],
                    "section_title": c["section_title"],
                    "document_title": c["document_title"],
                    "source_file": c["source_file"],
                    "topic": c["topic"],
                    "subtopic": c["subtopic"],
                    "type": c["type"],
                    "keywords": c["keywords"],
                    "complexity": c["complexity"],
                    "chunk_index": c["chunk_index"],
                    "total_chunks": c["total_chunks"],
                    "embedding_model": c["embedding_model"],
                    "engineering_context": c["engineering_context"],
                },
            )
            for c in batch
        ]
        client.upsert(collection_name=COLLECTION_NAME, points=points)
        log.info("  upserted %d/%d", min(batch_start + BATCH_SIZE, total), total)


# ---------------------------------------------------------------------------
# Validation query
# ---------------------------------------------------------------------------

def run_validation_query(client: QdrantClient) -> None:
    query = "pmsm speed oscillation tuning kp ki"
    log.info("\n%s\nValidation query: '%s'\n%s", "=" * 60, query, "=" * 60)
    vecs = embedding_function(query)
    if not vecs:
        log.error("Could not embed validation query.")
        return
    results = client.search(
        collection_name=COLLECTION_NAME,
        query_vector=vecs[0],
        limit=5,
        with_payload=True,
    )
    for rank, hit in enumerate(results, 1):
        p = hit.payload
        ctx = p.get("engineering_context", {})
        print(
            f"\n[{rank}] score={hit.score:.4f}  topic={p.get('topic')}  "
            f"type={p.get('type')}  complexity={p.get('complexity')}\n"
            f"    section : {p.get('section_title', '')[:80]}\n"
            f"    content : {p.get('content', '')[:200]} …\n"
            f"    problem : {ctx.get('problem', '')}\n"
            f"    scenario: {ctx.get('when_to_use', '')}\n"
            f"    params  : {ctx.get('related_parameters', [])}"
        )


# ---------------------------------------------------------------------------
# Main pipeline
# ---------------------------------------------------------------------------

def discover_html_files(directory: Path) -> list[Path]:
    files = sorted(directory.rglob("*.html"))
    log.info("Discovered %d HTML files in %s", len(files), directory)
    return files


def process_file(path: Path) -> list[dict[str, Any]]:
    parsed = parse_html_file(path)
    if not parsed:
        return []
    raw_chunks = build_chunks(parsed)
    total = len(raw_chunks)
    return [annotate_chunk(c, i, total) for i, c in enumerate(raw_chunks)]


def main() -> None:
    log.info("STM32 MC SDK RAG Ingestion Pipeline starting")

    # 1. Discover
    html_files = discover_html_files(HTML_DIR)
    if not html_files:
        log.error("No HTML files found in %s", HTML_DIR)
        return

    # 2. Parse + annotate
    all_chunks: list[dict[str, Any]] = []
    for i, path in enumerate(html_files):
        all_chunks.extend(process_file(path))
        if (i + 1) % 50 == 0:
            log.info("  parsed %d/%d files (%d chunks so far)", i + 1, len(html_files), len(all_chunks))

    log.info("Total annotated chunks: %d", len(all_chunks))
    if not all_chunks:
        log.error("No chunks produced. Aborting.")
        return

    # 3. Embed
    embedded = embed_chunks(all_chunks)
    log.info("Successfully embedded %d/%d chunks", len(embedded), len(all_chunks))

    # 4. Qdrant setup + upsert
    client = get_qdrant_client()
    ensure_collection(client)
    upsert_points(client, embedded)
    log.info("Ingestion complete. %d points in collection '%s'", len(embedded), COLLECTION_NAME)

    # 5. Validation
    run_validation_query(client)


if __name__ == "__main__":
    main()
