class BudgetExceeded(RuntimeError):
    code = "BUDGET_EXCEEDED"


class QuotaExceeded(BudgetExceeded):
    code = "QUOTA_EXCEEDED"


class BillingConfigurationError(RuntimeError):
    code = "BILLING_CONFIGURATION_REQUIRED"
