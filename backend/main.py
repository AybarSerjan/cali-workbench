from __future__ import annotations

import json
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, File, Form, HTTPException, Query, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import select
from sqlalchemy.orm import Session

from agent.graph import graph
from agent.tools.datasets import CapabilityUnavailable, registry
from backend.config import ROOT, ensure_directories, settings
from backend.db import SessionLocal, get_db, init_db
from backend.models import CalibrationCommit, Conversation, Dataset, MemoryFact, Message, Run, SourceFile, WikiArticle, WikiRevision, Workpackage
from backend.schemas import (
    A2lIndexCreate,
    CalibrationCommitCreate,
    CalibrationSessionCreate,
    ChatRequest,
    ConversationCreate,
    DatasetCreate,
    DatasetMergeRequest,
    DatasetUpdate,
    LinkSourceCreate,
    MemoryFactCreate,
    ModelSettings,
    ObfcmRequest,
    PublishRequest,
    SignalWindowRequest,
    SignalMappingCreate,
    WorkpackageEvaluateRequest,
    WorkpackageDraftRequest,
    WorkpackageMatchRequest,
    WorkpackageCreate,
    WorkpackageRevisionCreate,
    WorkpackageReview,
    WikiDraftCreate,
    WikiDraftUpdate,
)
from backend.services import a2l_index, llm, workpackages, wp_eval
from backend.services.analysis import compute_obfcm_result, find_signal_windows
from backend.services.calibration import (
    analyze_session,
    change_payload,
    compare_session,
    create_session,
    dataset_evolution_payload,
    get_session,
    list_sessions,
    match_a2l_for_hex,
    query_changes,
    session_payload,
    sync_wiki,
)
from backend.services.chat import cancel_action, confirm_action, send_message
from backend.services.datasets import add_file, commit_payload, create_calibration_commit, create_dataset, create_dataset_from_file, dataset_payload, delete_dataset, ensure_dataset_serials, merge_datasets, merge_output_path, merge_uploaded_mf4, save_merge_as_dataset, update_dataset
from backend.services.datasets import conversion_output_path, convert_uploaded_excel, save_conversion_as_dataset
from backend.services.files import delete_source, store_link_source, store_source
from backend.services.memory import add_fact, consolidate_conversation, context
from backend.services.wiki import article_payload, create_draft, export_okf_bundle, import_okf_bundle, publish, save_revision


@asynccontextmanager
async def lifespan(_app: FastAPI):
    ensure_directories()
    init_db()
    with SessionLocal() as session:
        ensure_dataset_serials(session)
    yield


app = FastAPI(title="CALI Platform", version="0.1.0", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://127.0.0.1:5173", "http://localhost:5173"],
    allow_methods=["*"],
    allow_headers=["*"],
)


def fail(exc: Exception) -> HTTPException:
    if isinstance(exc, CapabilityUnavailable):
        return HTTPException(501, str(exc))
    if isinstance(exc, RuntimeError):
        return HTTPException(409, str(exc))
    return HTTPException(400, str(exc))


@app.get("/api/health")
def health() -> dict:
    return {"ok": True, "service": "cali", "llm": llm.public_config(), "wiki_agent": llm.wiki_agent_public_config()}


@app.post("/api/sources")
def upload_source(file: UploadFile = File(...), session: Session = Depends(get_db)) -> dict:
    try:
        source = store_source(session, file.file, file.filename or "file", file.content_type or "")
        return source_payload(source)
    except Exception as exc:
        raise fail(exc) from exc


@app.post("/api/sources/link")
def add_link_source(value: LinkSourceCreate, session: Session = Depends(get_db)) -> dict:
    try:
        return source_payload(store_link_source(session, value.url, value.title, value.text))
    except Exception as exc:
        raise fail(exc) from exc


@app.get("/api/sources")
def list_sources(session: Session = Depends(get_db)) -> list[dict]:
    return [source_payload(item) for item in session.scalars(select(SourceFile).order_by(SourceFile.created_at.desc())).all()]


@app.delete("/api/sources/{source_id}")
def remove_source(source_id: str, session: Session = Depends(get_db)) -> dict:
    item = session.get(SourceFile, source_id)
    if not item:
        raise HTTPException(404, "知识源文件不存在")
    try:
        delete_source(session, item)
        return {"ok": True}
    except Exception as exc:
        raise fail(exc) from exc


def source_payload(item: SourceFile) -> dict:
    return {"id": item.id, "name": item.original_name, "suffix": item.suffix, "sha256": item.sha256, "size": item.size, "source_url": item.source_url, "created_at": item.created_at}


@app.post("/api/calibration/sessions")
def new_calibration_session(value: CalibrationSessionCreate, session: Session = Depends(get_db)) -> dict:
    try:
        item = create_session(session, value.a2l_file_id, value.hex_file_ids, value.created_by)
        return session_payload(session, item)
    except Exception as exc:
        raise fail(exc) from exc


@app.get("/api/calibration/sessions")
def calibration_sessions(
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=50, ge=1, le=500),
    sort: str = Query(default="desc", pattern="^(asc|desc)$"),
    session: Session = Depends(get_db),
) -> list[dict]:
    return [session_payload(session, item, include_changes=False) for item in list_sessions(session, offset, limit, sort)]


@app.get("/api/calibration/sessions/{session_id}")
def calibration_session(session_id: str, session: Session = Depends(get_db)) -> dict:
    item = get_session(session, session_id)
    if not item:
        raise HTTPException(404, "Calibration session does not exist")
    return session_payload(session, item)


@app.post("/api/calibration/sessions/{session_id}/compare")
def compare_calibration_session(session_id: str, session: Session = Depends(get_db)) -> dict:
    try:
        return session_payload(session, compare_session(session, session_id))
    except Exception as exc:
        raise fail(exc) from exc


@app.post("/api/calibration/sessions/{session_id}/analyze")
def analyze_calibration_session(session_id: str, session: Session = Depends(get_db)) -> dict:
    try:
        return session_payload(session, analyze_session(session, session_id))
    except Exception as exc:
        raise fail(exc) from exc


@app.get("/api/calibration/parameters")
def calibration_parameters(
    module: str | None = None,
    parameter_name: str | None = None,
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=100, ge=1, le=1000),
    session: Session = Depends(get_db),
) -> list[dict]:
    return [change_payload(item) for item in query_changes(session, module, parameter_name, offset, limit)]


@app.post("/api/calibration/wiki/sync")
def sync_calibration_wiki(session: Session = Depends(get_db)) -> dict:
    try:
        revisions = sync_wiki(session)
        return {"drafts_created": len(revisions), "revision_ids": [item.id for item in revisions]}
    except Exception as exc:
        raise fail(exc) from exc


@app.post("/api/a2l-index")
def new_a2l_index(value: A2lIndexCreate, session: Session = Depends(get_db)) -> dict:
    try:
        return a2l_index.build_index(session, value.dataset_file_id)
    except Exception as exc:
        raise fail(exc) from exc


@app.get("/api/a2l-index/{sha}/measurements")
def a2l_index_measurements(
    sha: str,
    q: str = "",
    limit: int = Query(default=50, ge=1, le=500),
    session: Session = Depends(get_db),
) -> dict:
    try:
        return {"sha256": sha, "measurements": a2l_index.search_measurements(sha, q, limit)}
    except Exception as exc:
        raise fail(exc) from exc


@app.get("/api/a2l-index/{sha}/trace")
def a2l_index_trace(
    sha: str,
    signal: str = Query(min_length=1),
    depth: int = Query(default=2, ge=1, le=4),
    session: Session = Depends(get_db),
) -> dict:
    try:
        return a2l_index.trace_query(sha, signal, depth)
    except Exception as exc:
        raise fail(exc) from exc


@app.post("/api/workpackages")
def new_workpackage(value: WorkpackageCreate, session: Session = Depends(get_db)) -> dict:
    try:
        item = workpackages.create_workpackage(
            session, value.wp_code, value.title, value.definition, value.created_by
        )
        return workpackages.workpackage_payload(session, item)
    except Exception as exc:
        session.rollback()
        raise fail(exc) from exc


@app.get("/api/workpackages")
def get_workpackages(session: Session = Depends(get_db)) -> list[dict]:
    return workpackages.list_workpackages(session)


@app.delete("/api/workpackages/{workpackage_id}")
def remove_workpackage(workpackage_id: str, session: Session = Depends(get_db)) -> dict:
    item = session.get(Workpackage, workpackage_id)
    if not item:
        raise HTTPException(404, "工作包不存在")
    try:
        workpackages.delete_workpackage(session, item)
        return {"ok": True}
    except Exception as exc:
        raise fail(exc) from exc


@app.post("/api/workpackages/draft")
def draft_workpackage(value: WorkpackageDraftRequest, session: Session = Depends(get_db)) -> dict:
    try:
        item = workpackages.draft_with_llm(session, value.wp_code, value.title, value.prompt)
        return workpackages.workpackage_payload(session, item)
    except Exception as exc:
        session.rollback()
        raise fail(exc) from exc


@app.post("/api/workpackages/import")
def import_workpackages(file: UploadFile = File(...), session: Session = Depends(get_db)) -> dict:
    try:
        items = workpackages.import_yaml(session, file.file.read(5 * 1024 * 1024 + 1))
        return {"imported": len(items), "workpackages": [workpackages.workpackage_payload(session, item) for item in items]}
    except Exception as exc:
        session.rollback()
        raise fail(exc) from exc


@app.get("/api/workpackages/export")
def export_workpackages(session: Session = Depends(get_db)) -> Response:
    return Response(
        content=workpackages.export_yaml(session),
        media_type="application/yaml",
        headers={"Content-Disposition": 'attachment; filename="cali-workpackages.yaml"'},
    )


@app.get("/api/workpackages/overview")
def workpackage_overview(session: Session = Depends(get_db)) -> list[dict]:
    return wp_eval.completion_overview(session)


@app.post("/api/workpackages/evaluate-all")
def evaluate_all_workpackages(value: WorkpackageEvaluateRequest, session: Session = Depends(get_db)) -> dict:
    try:
        return wp_eval.evaluate_all(session, value.dataset_id, value.a2l_file_id, value.hex_file_id)
    except Exception as exc:
        session.rollback()
        raise fail(exc) from exc


@app.get("/api/workpackages/{workpackage_id}")
def get_workpackage(workpackage_id: str, session: Session = Depends(get_db)) -> dict:
    try:
        return workpackages.workpackage_payload(session, workpackages.get_workpackage(session, workpackage_id))
    except Exception as exc:
        raise fail(exc) from exc


@app.put("/api/workpackages/{workpackage_id}/revision")
def new_workpackage_revision(
    workpackage_id: str,
    value: WorkpackageRevisionCreate,
    session: Session = Depends(get_db),
) -> dict:
    try:
        workpackages.create_revision(session, workpackage_id, value.definition, value.title, value.created_by)
        return workpackages.workpackage_payload(session, workpackages.get_workpackage(session, workpackage_id))
    except Exception as exc:
        session.rollback()
        raise fail(exc) from exc


@app.post("/api/workpackages/{workpackage_id}/review")
def review_workpackage(
    workpackage_id: str,
    value: WorkpackageReview,
    session: Session = Depends(get_db),
) -> dict:
    try:
        workpackages.review_latest(session, workpackage_id, value.review_notes)
        return workpackages.workpackage_payload(session, workpackages.get_workpackage(session, workpackage_id))
    except Exception as exc:
        session.rollback()
        raise fail(exc) from exc


@app.post("/api/workpackages/{workpackage_id}/approve")
def approve_workpackage(workpackage_id: str, session: Session = Depends(get_db)) -> dict:
    try:
        workpackages.approve_latest(session, workpackage_id)
        return workpackages.workpackage_payload(session, workpackages.get_workpackage(session, workpackage_id))
    except Exception as exc:
        session.rollback()
        raise fail(exc) from exc


@app.post("/api/workpackages/{workpackage_id}/match")
def match_workpackage_signals(workpackage_id: str, value: WorkpackageMatchRequest, session: Session = Depends(get_db)) -> dict:
    try:
        return wp_eval.match_signals(session, workpackage_id, value.dataset_id, value.a2l_sha256)
    except Exception as exc:
        session.rollback()
        raise fail(exc) from exc


@app.post("/api/workpackages/{workpackage_id}/evaluate")
def evaluate_workpackage(workpackage_id: str, value: WorkpackageEvaluateRequest, session: Session = Depends(get_db)) -> dict:
    try:
        return wp_eval.evaluation_payload(wp_eval.evaluate(
            session, workpackage_id, value.dataset_id, value.a2l_file_id, value.hex_file_id
        ))
    except Exception as exc:
        session.rollback()
        raise fail(exc) from exc


@app.get("/api/signal-map")
def get_signal_mappings(match_status: str | None = None, session: Session = Depends(get_db)) -> list[dict]:
    return wp_eval.list_mappings(session, match_status)


@app.post("/api/signal-map")
def add_signal_mapping(value: SignalMappingCreate, session: Session = Depends(get_db)) -> dict:
    try:
        return wp_eval.mapping_payload(wp_eval.create_manual_mapping(
            session, value.a2l_name, value.mf4_channel, value.evidence
        ))
    except Exception as exc:
        session.rollback()
        raise fail(exc) from exc


@app.post("/api/signal-map/{mapping_id}/confirm")
def confirm_signal_mapping(mapping_id: str, session: Session = Depends(get_db)) -> dict:
    try:
        return wp_eval.mapping_payload(wp_eval.confirm_mapping(session, mapping_id))
    except Exception as exc:
        session.rollback()
        raise fail(exc) from exc


@app.get("/api/wp-evaluations")
def get_wp_evaluations(
    workpackage_id: str | None = None,
    dataset_id: str | None = None,
    session: Session = Depends(get_db),
) -> list[dict]:
    from backend.models import WpEvaluation
    query = select(WpEvaluation)
    if workpackage_id: query = query.where(WpEvaluation.workpackage_id == workpackage_id)
    if dataset_id: query = query.where(WpEvaluation.dataset_id == dataset_id)
    items = session.scalars(query.order_by(WpEvaluation.created_at.desc())).all()
    return [wp_eval.evaluation_payload(item) for item in items]


@app.get("/api/wp-evaluations/{evaluation_id}")
def get_wp_evaluation(evaluation_id: str, session: Session = Depends(get_db)) -> dict:
    from backend.models import WpEvaluation
    item = session.get(WpEvaluation, evaluation_id)
    if not item: raise HTTPException(404, "WP evaluation not found")
    return wp_eval.evaluation_payload(item)


@app.post("/api/wiki/drafts")
def new_wiki_draft(value: WikiDraftCreate, session: Session = Depends(get_db)) -> dict:
    try:
        revision = create_draft(session, value.source_id, value.title, value.slug)
        return article_payload(session, revision.article)
    except Exception as exc:
        raise fail(exc) from exc


@app.put("/api/wiki/revisions/{revision_id}")
def edit_wiki_draft(revision_id: str, value: WikiDraftUpdate, session: Session = Depends(get_db)) -> dict:
    try:
        revision = save_revision(session, revision_id, value.content)
        return article_payload(session, revision.article)
    except Exception as exc:
        raise fail(exc) from exc


@app.post("/api/wiki/revisions/{revision_id}/publish")
def publish_wiki_revision(revision_id: str, value: PublishRequest, session: Session = Depends(get_db)) -> dict:
    try:
        revision = publish(session, revision_id, value.expected_base_sha256)
        return article_payload(session, revision.article)
    except Exception as exc:
        raise fail(exc) from exc


@app.get("/api/wiki")
def list_wiki(session: Session = Depends(get_db)) -> list[dict]:
    articles = session.scalars(select(WikiArticle).order_by(WikiArticle.updated_at.desc())).all()
    return [article_payload(session, item) for item in articles]


@app.post("/api/wiki/import")
def import_wiki_bundle(file: UploadFile = File(...), session: Session = Depends(get_db)) -> dict:
    try:
        content = file.file.read(50 * 1024 * 1024 + 1)
        revisions = import_okf_bundle(session, content)
        articles = {revision.article.id: revision.article for revision in revisions}
        return {"imported": len(revisions), "articles": [article_payload(session, article) for article in articles.values()]}
    except Exception as exc:
        session.rollback()
        raise fail(exc) from exc


@app.get("/api/wiki/export")
def export_wiki_bundle() -> Response:
    return Response(
        content=export_okf_bundle(),
        media_type="application/zip",
        headers={"Content-Disposition": 'attachment; filename="cali-wiki-okf.zip"'},
    )


@app.post("/api/datasets")
def new_dataset(value: DatasetCreate, session: Session = Depends(get_db)) -> dict:
    try:
        return dataset_payload(create_dataset(session, value.name, value.kind, value.description))
    except Exception as exc:
        raise fail(exc) from exc


@app.post("/api/datasets/files")
def upload_dataset_as_dataset(file: UploadFile = File(...), kind: str = Form("mdf"), description: str = Form("数据分析"), session: Session = Depends(get_db)) -> dict:
    try:
        dataset, duplicate = create_dataset_from_file(session, file.file, file.filename or "file", kind, description)
        payload = dataset_payload(dataset)
        payload["duplicate"] = duplicate
        return payload
    except Exception as exc:
        raise fail(exc) from exc


@app.get("/api/datasets")
def list_datasets(session: Session = Depends(get_db)) -> list[dict]:
    ensure_dataset_serials(session)
    items = session.scalars(select(Dataset).order_by(Dataset.created_at.desc())).all()
    return [dataset_payload(item) for item in items]


@app.post("/api/datasets/merge")
def merge_catalogued_datasets(value: DatasetMergeRequest, session: Session = Depends(get_db)) -> dict:
    try:
        pairs = [(item.signal_a, item.signal_b) for item in value.signal_pairs]
        dataset = merge_datasets(
            session, value.dataset_a_id, value.dataset_b_id, value.name, pairs or None
        )
        return dataset_payload(dataset)
    except Exception as exc:
        raise fail(exc) from exc


@app.patch("/api/datasets/{dataset_id}")
def edit_dataset(dataset_id: str, value: DatasetUpdate, session: Session = Depends(get_db)) -> dict:
    dataset = session.get(Dataset, dataset_id)
    if not dataset:
        raise HTTPException(404, "数据集不存在")
    try:
        ensure_dataset_serials(session)
        return dataset_payload(update_dataset(session, dataset, **value.model_dump()))
    except Exception as exc:
        raise fail(exc) from exc


@app.delete("/api/datasets/{dataset_id}")
def remove_dataset(dataset_id: str, session: Session = Depends(get_db)) -> dict:
    dataset = session.get(Dataset, dataset_id)
    if not dataset:
        raise HTTPException(404, "数据集不存在")
    try:
        delete_dataset(session, dataset)
        return {"ok": True}
    except Exception as exc:
        raise fail(exc) from exc


@app.post("/api/datasets/{dataset_id}/files")
def upload_dataset_file(dataset_id: str, file: UploadFile = File(...), session: Session = Depends(get_db)) -> dict:
    dataset = session.get(Dataset, dataset_id)
    if not dataset:
        raise HTTPException(404, "数据集不存在")
    try:
        ensure_dataset_serials(session)
        add_file(session, dataset, file.file, file.filename or "file")
        session.refresh(dataset)
        return dataset_payload(dataset)
    except Exception as exc:
        raise fail(exc) from exc


@app.get("/api/datasets/{dataset_id}/commits")
def list_calibration_commits(dataset_id: str, session: Session = Depends(get_db)) -> list[dict]:
    dataset = session.get(Dataset, dataset_id)
    if not dataset:
        raise HTTPException(404, "数据集不存在")
    commits = session.scalars(
        select(CalibrationCommit).where(CalibrationCommit.dataset_id == dataset_id).order_by(CalibrationCommit.created_at.desc())
    ).all()
    return [commit_payload(session, item) for item in commits]


@app.get("/api/datasets/{dataset_id}/a2l-match/{hex_file_id}")
def match_calibration_a2l(dataset_id: str, hex_file_id: str, session: Session = Depends(get_db)) -> dict:
    dataset = session.get(Dataset, dataset_id)
    if not dataset:
        raise HTTPException(404, "数据集不存在")
    try:
        return match_a2l_for_hex(session, dataset, hex_file_id)
    except Exception as exc:
        raise fail(exc) from exc


@app.post("/api/datasets/{dataset_id}/commits")
def new_calibration_commit(dataset_id: str, value: CalibrationCommitCreate, session: Session = Depends(get_db)) -> dict:
    dataset = session.get(Dataset, dataset_id)
    if not dataset:
        raise HTTPException(404, "数据集不存在")
    try:
        commit = create_calibration_commit(session, dataset, value.parent_id, value.a2l_file_id, value.hex_file_id)
        return commit_payload(session, commit)
    except Exception as exc:
        raise fail(exc) from exc


@app.get("/api/datasets/{dataset_id}/evolution")
def calibration_dataset_evolution(dataset_id: str, session: Session = Depends(get_db)) -> dict:
    dataset = session.get(Dataset, dataset_id)
    if not dataset:
        raise HTTPException(404, "数据集不存在")
    try:
        return dataset_evolution_payload(session, dataset)
    except Exception as exc:
        raise fail(exc) from exc


@app.post("/api/datasets/{dataset_id}/analysis/windows")
def analyze_signal_windows(dataset_id: str, value: SignalWindowRequest, session: Session = Depends(get_db)) -> dict:
    dataset = session.get(Dataset, dataset_id)
    if not dataset:
        raise HTTPException(404, "数据集不存在")
    try:
        return find_signal_windows(dataset, value.signal, value.op, value.threshold, value.min_duration)
    except Exception as exc:
        raise fail(exc) from exc


@app.post("/api/datasets/{dataset_id}/analysis/obfcm")
def analyze_obfcm(dataset_id: str, value: ObfcmRequest, session: Session = Depends(get_db)) -> dict:
    dataset = session.get(Dataset, dataset_id)
    if not dataset:
        raise HTTPException(404, "数据集不存在")
    try:
        return compute_obfcm_result(dataset, value.channel_map)
    except Exception as exc:
        raise fail(exc) from exc


@app.post("/api/datasets/{dataset_id}/tools/{tool_name}")
def call_dataset_tool(dataset_id: str, tool_name: str, payload: dict, session: Session = Depends(get_db)) -> dict:
    dataset = session.get(Dataset, dataset_id)
    if not dataset:
        raise HTTPException(404, "数据集不存在")
    adapter = registry.get(dataset.kind)
    paths = [ROOT / item.relative_path for item in dataset.files]
    try:
        if tool_name == "inspect":
            return adapter.inspect(paths)
        if tool_name == "metadata":
            return adapter.read_metadata(paths)
        if tool_name == "channels":
            return {"channels": adapter.list_channels(paths)}
        if tool_name == "window":
            return adapter.read_window(paths, str(payload.get("channel", "")), float(payload.get("start", 0)), float(payload.get("end", 0)))
        if tool_name == "compare":
            return adapter.compare_versions(paths)
        if tool_name == "query":
            return adapter.query_value(paths, str(payload.get("name", "")))
        raise ValueError("未知数据工具")
    except Exception as exc:
        raise fail(exc) from exc


@app.post("/api/tools/merge_mf4")
def tool_merge_mf4(file_a: UploadFile = File(...), file_b: UploadFile = File(...), name: str = Form(""), session: Session = Depends(get_db)) -> dict:
    try:
        return merge_uploaded_mf4(file_a.file, file_a.filename or "a.mf4", file_b.file, file_b.filename or "b.mf4", name)
    except Exception as exc:
        raise fail(exc) from exc


@app.get("/api/tools/merge_mf4/{token}/download")
def tool_merge_mf4_download(token: str):
    try:
        path = merge_output_path(token)
    except (ValueError, FileNotFoundError) as exc:
        raise HTTPException(404, str(exc)) from exc
    return FileResponse(path, filename=path.name, media_type="application/octet-stream")


@app.post("/api/tools/merge_mf4/{token}/save")
def tool_merge_mf4_save(token: str, name: str = "", session: Session = Depends(get_db)) -> dict:
    try:
        return dataset_payload(save_merge_as_dataset(session, token, name))
    except Exception as exc:
        raise fail(exc) from exc


@app.post("/api/tools/excel_to_mf4")
def tool_excel_to_mf4(file: UploadFile = File(...), sample_rate: float = Form(1.0), units_row: str = Form("auto"), name: str = Form(""), session: Session = Depends(get_db)) -> dict:
    try:
        return convert_uploaded_excel(file.file, file.filename or "data.xlsx", name, sample_rate, units_row)
    except Exception as exc:
        raise fail(exc) from exc


@app.get("/api/tools/excel_to_mf4/{token}/download")
def tool_excel_to_mf4_download(token: str):
    try:
        path = conversion_output_path(token)
    except (ValueError, FileNotFoundError) as exc:
        raise HTTPException(404, str(exc)) from exc
    return FileResponse(path, filename=path.name, media_type="application/octet-stream")


@app.post("/api/tools/excel_to_mf4/{token}/save")
def tool_excel_to_mf4_save(token: str, name: str = "", session: Session = Depends(get_db)) -> dict:
    try:
        return dataset_payload(save_conversion_as_dataset(session, token, name))
    except Exception as exc:
        raise fail(exc) from exc


@app.post("/api/conversations")
def new_conversation(value: ConversationCreate, session: Session = Depends(get_db)) -> dict:
    item = Conversation(title=value.title)
    session.add(item)
    session.commit()
    session.refresh(item)
    return conversation_payload(session, item)


@app.get("/api/conversations")
def list_conversations(include_archived: bool = False, session: Session = Depends(get_db)) -> list[dict]:
    query = select(Conversation).order_by(Conversation.updated_at.desc())
    if not include_archived:
        query = query.where(Conversation.archived.is_(False))
    items = session.scalars(query).all()
    return [conversation_payload(session, item) for item in items]


def conversation_payload(session: Session, item: Conversation) -> dict:
    messages = session.scalars(
        select(Message).where(Message.conversation_id == item.id).order_by(Message.turn_number, Message.created_at)
    ).all()
    return {
        "id": item.id,
        "title": item.title,
        "archived": item.archived,
        "created_at": item.created_at,
        "updated_at": item.updated_at,
        "summaries": context(session, item.id)["summaries"],
        "messages": [
            {
                "id": message.id,
                "turn": message.turn_number,
                "role": message.role,
                "content": message.content,
                "sources": json.loads(message.sources_json or "[]"),
                "meta": json.loads(message.meta_json or "{}"),
                "created_at": message.created_at,
            }
            for message in messages
        ],
    }


@app.post("/api/conversations/{conversation_id}/messages")
def chat_message(conversation_id: str, value: ChatRequest, session: Session = Depends(get_db)) -> dict:
    conversation = session.get(Conversation, conversation_id)
    if not conversation:
        raise HTTPException(404, "对话不存在")
    try:
        return send_message(session, conversation, value.content, grill=value.grill)
    except Exception as exc:
        raise fail(exc) from exc


@app.post("/api/conversations/{conversation_id}/messages/{message_id}/confirm")
def confirm_message_action(conversation_id: str, message_id: str, session: Session = Depends(get_db)) -> dict:
    conversation = session.get(Conversation, conversation_id)
    if not conversation:
        raise HTTPException(404, "对话不存在")
    try:
        return confirm_action(session, conversation, message_id)
    except Exception as exc:
        raise fail(exc) from exc


@app.post("/api/conversations/{conversation_id}/messages/{message_id}/cancel")
def cancel_message_action(conversation_id: str, message_id: str, session: Session = Depends(get_db)) -> dict:
    conversation = session.get(Conversation, conversation_id)
    if not conversation:
        raise HTTPException(404, "对话不存在")
    try:
        return cancel_action(session, conversation, message_id)
    except Exception as exc:
        raise fail(exc) from exc


@app.post("/api/conversations/{conversation_id}/messages/stream")
def chat_message_stream(conversation_id: str, value: ChatRequest, session: Session = Depends(get_db)):
    conversation = session.get(Conversation, conversation_id)
    if not conversation:
        raise HTTPException(404, "对话不存在")
    result = send_message(session, conversation, value.content)

    def events():
        for line in result["answer"].splitlines(keepends=True):
            yield f"data: {json.dumps({'type': 'delta', 'content': line}, ensure_ascii=False)}\n\n"
        yield f"data: {json.dumps({'type': 'done', **result}, ensure_ascii=False)}\n\n"

    return StreamingResponse(events(), media_type="text/event-stream")


@app.post("/api/conversations/{conversation_id}/archive")
def archive_conversation(conversation_id: str, session: Session = Depends(get_db)) -> dict:
    conversation = session.get(Conversation, conversation_id)
    if not conversation:
        raise HTTPException(404, "对话不存在")
    facts = consolidate_conversation(session, conversation_id)
    conversation.archived = True
    session.commit()
    return {"ok": True, "facts_created": len(facts)}


@app.get("/api/memory/facts")
def list_facts(session: Session = Depends(get_db)) -> list[dict]:
    items = session.scalars(select(MemoryFact).order_by(MemoryFact.created_at.desc())).all()
    return [{"id": x.id, "key": x.key, "value": x.value, "status": x.status, "supersedes_id": x.supersedes_id, "created_at": x.created_at} for x in items]


@app.post("/api/memory/facts")
def new_fact(value: MemoryFactCreate, session: Session = Depends(get_db)) -> dict:
    try:
        fact = add_fact(session, value.key, value.value, conversation_id=value.conversation_id, supersedes_id=value.supersedes_id, source_turn=value.source_turn)
        return {"id": fact.id, "key": fact.key, "value": fact.value, "status": fact.status}
    except Exception as exc:
        raise fail(exc) from exc


@app.get("/api/runs")
def list_runs(session: Session = Depends(get_db)) -> list[dict]:
    items = session.scalars(select(Run).order_by(Run.created_at.desc()).limit(100)).all()
    return [{"id": x.id, "kind": x.kind, "status": x.status, "current_node": x.current_node, "error": x.error, "created_at": x.created_at, "updated_at": x.updated_at} for x in items]


@app.get("/api/conversations/{conversation_id}/checkpoints")
def checkpoints(conversation_id: str) -> list[dict]:
    try:
        history = graph.get_state_history({"configurable": {"thread_id": conversation_id}})
        return [{"config": item.config, "next": list(item.next), "metadata": item.metadata, "tasks": [str(task) for task in item.tasks]} for item in history]
    except Exception as exc:
        raise fail(exc) from exc


@app.get("/api/settings/model")
def model_settings() -> dict:
    return llm.public_config()


@app.put("/api/settings/model")
def update_model_settings(value: ModelSettings) -> dict:
    llm.configure(value)
    return llm.public_config()


@app.post("/api/settings/model/test")
async def test_model() -> dict:
    try:
        return await llm.test_connection()
    except Exception as exc:
        raise fail(exc) from exc


@app.get("/api/settings/wiki-agent")
def wiki_agent_settings() -> dict:
    return llm.wiki_agent_public_config()


@app.post("/api/settings/wiki-agent/test")
def test_wiki_agent() -> dict:
    try:
        return llm.wiki_agent_test()
    except Exception as exc:
        raise fail(exc) from exc


frontend = ROOT / "frontend" / "dist"
if frontend.exists():
    app.mount("/assets", StaticFiles(directory=frontend / "assets"), name="assets")

    @app.get("/{path:path}", include_in_schema=False)
    def spa(path: str):
        candidate = (frontend / path).resolve()
        if path and candidate.is_relative_to(frontend.resolve()) and candidate.is_file():
            return FileResponse(candidate)
        return FileResponse(frontend / "index.html")


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("backend.main:app", host=settings.host, port=settings.port, reload=False)
