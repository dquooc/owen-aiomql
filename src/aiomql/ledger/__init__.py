"""Deal Ledger models, normalization, and explicit storage contracts."""

from .models import AccountRef, LedgerCheckpoint, NormalizedDeal, RecoveryRange
from .normalize import DealNormalizationError, NORMALIZER_VERSION, normalize_deal
from .repository import AccountIdentityError, DealObservation, DealRepository, IngestOutcome, IngestResult
from .repository import AccountLookupRepository
from .repository import CheckpointRegressionError, CheckpointRepository
from .account_lookup import verify_account_identity
from .sqlite import SQLiteDealRepository
from .source import DealHistoryBatch, DealHistorySource, HistoryFetchError, MetaTraderDealHistorySource, SessionAccount
from .ingestion import DealIngestor, IngestionError, IngestionReport, IngestionStage, IngestionStatus
from .recovery import plan_recovery_range
from .recovery_models import RecoveryOutcome, RecoveryResult
from .recovery_config import Clock, RecoveryConfig, utc_now
from .recovery_manager import RecoveryAttempt, RecoveryManager

__all__ = ["AccountRef", "LedgerCheckpoint", "NormalizedDeal", "RecoveryRange", "DealNormalizationError", "NORMALIZER_VERSION", "normalize_deal",
           "AccountIdentityError", "DealObservation", "DealRepository", "IngestOutcome", "IngestResult",
           "CheckpointRegressionError", "CheckpointRepository",
           "SQLiteDealRepository", "DealHistoryBatch", "DealHistorySource", "HistoryFetchError",
           "MetaTraderDealHistorySource", "SessionAccount", "DealIngestor", "IngestionError",
           "IngestionReport", "IngestionStage", "IngestionStatus", "plan_recovery_range",
           "RecoveryOutcome", "RecoveryResult", "AccountLookupRepository", "verify_account_identity",
           "RecoveryConfig", "Clock", "utc_now", "RecoveryAttempt", "RecoveryManager"]
