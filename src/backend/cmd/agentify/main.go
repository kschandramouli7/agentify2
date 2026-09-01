package main

import (
	"context"
	"log/slog"
	"net/http"
	"os"
	"os/signal"
	"syscall"
	"time"

	"github.com/aws/aws-sdk-go-v2/config"
	"github.com/aws/aws-sdk-go-v2/service/dynamodb"
	"github.com/aws/aws-sdk-go-v2/service/secretsmanager"

	"github.com/chan/agentify/backend/internal/api"
	apicfg "github.com/chan/agentify/backend/internal/config"
	"github.com/chan/agentify/backend/internal/governance"
	"github.com/chan/agentify/backend/internal/orchestrator"
	"github.com/chan/agentify/backend/internal/retention"
	"github.com/chan/agentify/backend/internal/secrets"
	"github.com/chan/agentify/backend/internal/telemetry"
)

// Package main: entry point for the agentify backend service.
func main() {
	// Setup logging
	logger := telemetry.NewLogger()
	slog.SetDefault(logger)

	// Load config
	cfg, err := apicfg.LoadFromEnv()
	if err != nil {
		logger.Error("failed to load config", "error", err)
		os.Exit(1)
	}

	// Initialize the pod-registry backend. In "memory" mode we skip AWS entirely
	// and run the registry in-process (local dev / no credentials); otherwise we
	// connect a DynamoDB client. The same AWS config is reused for the
	// Secrets Manager client (ADR 0025) when INTEGRATION_SECRETS_PREFIX is set,
	// so a deployment needs at most one AWS config load regardless of which
	// of these two features it uses.
	var dbClient *dynamodb.Client
	var secretsMgr secrets.Manager
	if cfg.RegistryBackend == "memory" && cfg.IntegrationSecretsPrefix == "" {
		logger.Info("registry backend: in-memory (AWS not used)")
	} else {
		awsCfg, err := config.LoadDefaultConfig(context.Background(), config.WithRegion(cfg.AWSRegion))
		if err != nil {
			logger.Error("failed to load AWS config", "error", err)
			os.Exit(1)
		}
		if cfg.RegistryBackend != "memory" {
			dbClient = dynamodb.NewFromConfig(awsCfg)
		} else {
			logger.Info("registry backend: in-memory (AWS not used)")
		}
		if cfg.IntegrationSecretsPrefix != "" {
			secretsMgr = secrets.NewAWSManager(secretsmanager.NewFromConfig(awsCfg))
			logger.Info("integration token secrets manager enabled", "prefix", cfg.IntegrationSecretsPrefix)
		}
	}

	// Initialize orchestrator with DynamoDB
	orchCfg := &orchestrator.Config{Config: cfg}
	orch, err := orchestrator.New(orchCfg, dbClient, logger)
	if err != nil {
		logger.Error("failed to initialize orchestrator", "error", err)
		os.Exit(1)
	}

	// Egress data-governance redactor (ADR 0007) — minimizes data sent to the agent/model.
	redactor := governance.NewRedactor(cfg.RedactionEnabled, cfg.RedactionPseudonymize, logger)
	logger.Info("egress redaction configured", "enabled", cfg.RedactionEnabled, "pseudonymize", cfg.RedactionPseudonymize)

	// Events-table retention janitor (ADR 0015). Disabled if days<=0 or the
	// relational backend doesn't support purging (e.g. unprovisioned).
	janitorCtx, stopJanitor := context.WithCancel(context.Background())
	defer stopJanitor()
	if relational, err := orch.GetBackendFactory().GetBackend("relational"); err == nil {
		if purger, ok := relational.(retention.Purger); ok {
			if j := retention.New(purger, cfg.EventsRetentionDays, cfg.EventsRetentionIntervalMinutes, logger); j != nil {
				go j.Run(janitorCtx)
			}
		} else {
			logger.Warn("relational backend does not support retention; janitor disabled")
		}
	}

	// Wire the integration + trace + pricing stores via type assertions on the relational backend.
	// All are nil when Postgres is not provisioned (memory mode).
	var integrationStore api.IntegrationStore
	var traceStore api.TraceStore
	var pricingStore api.PricingStore
	var chatStore api.ChatStore
	var remediationStore api.RemediationStore
	var serviceDepsStore api.ServiceDependencyStore
	var clusterServiceStore api.ClusterServiceStore
	var clusterIngressStore api.ClusterIngressStore
	var clusterHealthStore api.ClusterHealthStore
	if relational, err := orch.GetBackendFactory().GetBackend("relational"); err == nil {
		if store, ok := relational.(api.IntegrationStore); ok {
			integrationStore = store
		}
		if store, ok := relational.(api.TraceStore); ok {
			traceStore = store
		}
		if store, ok := relational.(api.PricingStore); ok {
			pricingStore = store
		}
		if store, ok := relational.(api.ChatStore); ok {
			chatStore = store
		}
		if store, ok := relational.(api.RemediationStore); ok {
			remediationStore = store
		}
		if store, ok := relational.(api.ServiceDependencyStore); ok {
			serviceDepsStore = store
		}
		if store, ok := relational.(api.ClusterServiceStore); ok {
			clusterServiceStore = store
		}
		if store, ok := relational.(api.ClusterIngressStore); ok {
			clusterIngressStore = store
		}
		if store, ok := relational.(api.ClusterHealthStore); ok {
			clusterHealthStore = store
		}
	}

	// Phase-3 remediation config (ADR 0020 / spec 011 Use Cases 1+2). Every
	// write action is proposed first and only executed after an explicit
	// human approval — never auto-executed regardless of confidence.
	remediationCfg := api.RemediationConfig{
		ProposalTTL: time.Duration(cfg.RemediationProposalTTLMinutes) * time.Minute,
		AuthToken:   cfg.RemediationAuthToken,
	}

	// Build the API handler once; the router and the proactive investigation loop
	// (ADR 0016) share it.
	handler := api.NewHandler(orch, cfg.AgentServiceURL, redactor, integrationStore, traceStore, pricingStore, chatStore, remediationStore, remediationCfg, serviceDepsStore, clusterServiceStore, clusterIngressStore, clusterHealthStore, secretsMgr, cfg.IntegrationSecretsPrefix, logger)
	// Version-pinned evaluation endpoint (ADR 0030). Set after construction
	// because NewHandler already takes sixteen parameters.
	handler.SetEvalAuthToken(cfg.EvalAuthToken, cfg.Env)

	// Proactive investigation loop (spec 009). Opt-in: requires INVESTIGATION_ENABLED
	// and a webhook URL; otherwise the loop never starts.
	if cfg.InvestigationEnabled && cfg.InvestigationWebhookURL != "" {
		inv := api.NewInvestigator(handler, api.NewWebhookNotifier(cfg.InvestigationWebhookURL), api.InvestigationConfig{
			SweepInterval:    time.Duration(cfg.InvestigationSweepIntervalMinutes) * time.Minute,
			MaxPerSweep:      cfg.InvestigationMaxPerSweep,
			Cooldown:         time.Duration(cfg.InvestigationCooldownMinutes) * time.Minute,
			CertCriticalDays: cfg.InvestigationCertCriticalDays,
			// Phase-3 remediation (ADR 0020) — opt-in, off by default. When
			// enabled, a DEGRADED/UNHEALTHY diagnosis also produces a pending
			// remediation proposal; it is never auto-executed.
			RemediationEnabled: cfg.AutonomousRemediationEnabled,
			ProposalTTL:        remediationCfg.ProposalTTL,
		}, logger)
		go inv.Run(janitorCtx)
		logger.Info("proactive investigation loop enabled",
			"interval_minutes", cfg.InvestigationSweepIntervalMinutes,
			"remediation_enabled", cfg.AutonomousRemediationEnabled)
	} else {
		logger.Info("proactive investigation loop disabled (opt-in)")
	}

	// Deployment Guardian poller (spec 011 Use Case 2). Opt-in: requires
	// DEPLOY_GUARDIAN_ENABLED; proposes rollback on post-deploy degradation,
	// never auto-executes (ADR 0020).
	if cfg.DeployGuardianEnabled {
		dg := api.NewDeploymentGuardian(handler, api.DeploymentGuardianConfig{
			PollInterval: time.Duration(cfg.DeployGuardianPollIntervalMinutes) * time.Minute,
			SettleAfter:  time.Duration(cfg.DeployGuardianSettleSeconds) * time.Second,
			ProposalTTL:  remediationCfg.ProposalTTL,
		}, logger)
		go dg.Run(janitorCtx)
		logger.Info("deployment guardian enabled",
			"poll_interval_minutes", cfg.DeployGuardianPollIntervalMinutes)
	} else {
		logger.Info("deployment guardian disabled (opt-in)")
	}

	// Setup HTTP server
	router := api.NewRouter(handler, logger)
	server := &http.Server{
		Addr:        cfg.Port,
		Handler:     router,
		ReadTimeout: 15 * time.Second,
		// Tier-2 Opus calls take 30-90s. WriteTimeout must exceed the longest
		// agent call plus response serialisation — set to 3 min with headroom.
		WriteTimeout: 180 * time.Second,
		IdleTimeout:  120 * time.Second,
	}

	// Start server in a goroutine
	go func() {
		logger.Info("starting server", "addr", cfg.Port)
		if err := server.ListenAndServe(); err != nil && err != http.ErrServerClosed {
			logger.Error("server error", "error", err)
		}
	}()

	// Graceful shutdown on SIGTERM / SIGINT
	sigch := make(chan os.Signal, 1)
	signal.Notify(sigch, syscall.SIGTERM, syscall.SIGINT)
	<-sigch

	logger.Info("shutting down gracefully...")
	ctx, cancel := context.WithTimeout(context.Background(), 30*time.Second)
	defer cancel()

	if err := server.Shutdown(ctx); err != nil {
		logger.Error("shutdown error", "error", err)
		os.Exit(1)
	}

	// Stop the retention janitor before closing the DB it writes to.
	stopJanitor()

	// Release storage backend connections.
	if err := orch.Close(); err != nil {
		logger.Error("error closing storage backends", "error", err)
	}

	logger.Info("shutdown complete")
}
