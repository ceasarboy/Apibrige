CREATE TABLE api_connections (
	id UUID NOT NULL, 
	name VARCHAR(100) NOT NULL, 
	connection_type VARCHAR(20) NOT NULL, 
	protocol VARCHAR(20) NOT NULL, 
	base_url VARCHAR(500), 
	auth_type VARCHAR(20), 
	auth_config JSON, 
	headers JSON, 
	timeout_ms INTEGER NOT NULL, 
	retry_count INTEGER NOT NULL, 
	db_type VARCHAR(20), 
	db_config JSON, 
	status VARCHAR(20) NOT NULL, 
	created_at TIMESTAMP WITHOUT TIME ZONE NOT NULL, 
	updated_at TIMESTAMP WITHOUT TIME ZONE NOT NULL, 
	PRIMARY KEY (id)
);

CREATE TABLE field_mappings (
	id UUID NOT NULL, 
	name VARCHAR(100) NOT NULL, 
	source_connection_id UUID NOT NULL, 
	target_connection_id UUID NOT NULL, 
	source_endpoint VARCHAR(200), 
	target_endpoint VARCHAR(200), 
	source_table VARCHAR(100), 
	target_table VARCHAR(100), 
	source_headers JSON, 
	target_headers JSON, 
	mapping_config JSON NOT NULL, 
	transform_script TEXT, 
	sync_mode VARCHAR(20) NOT NULL, 
	incremental_field VARCHAR(100), 
	batch_size INTEGER NOT NULL, 
	status VARCHAR(20) NOT NULL, 
	created_at TIMESTAMP WITHOUT TIME ZONE NOT NULL, 
	updated_at TIMESTAMP WITHOUT TIME ZONE NOT NULL, 
	PRIMARY KEY (id), 
	FOREIGN KEY(source_connection_id) REFERENCES api_connections (id), 
	FOREIGN KEY(target_connection_id) REFERENCES api_connections (id)
);

CREATE TABLE probe_sessions (
	id UUID NOT NULL, 
	status VARCHAR(30) NOT NULL, 
	created_via VARCHAR(10) NOT NULL, 
	source_snapshot JSON, 
	target_snapshot JSON, 
	source_connection_id UUID, 
	target_connection_id UUID, 
	source_endpoint VARCHAR(200), 
	target_endpoint VARCHAR(200), 
	source_schema JSON, 
	target_schema JSON, 
	mapping_draft JSON, 
	draft_issues JSON, 
	source_sample JSON, 
	l1_result JSON, 
	l2_result JSON, 
	l3_result JSON, 
	e2e_authorized BOOLEAN NOT NULL, 
	authorized_by VARCHAR(100), 
	probe_calls JSON, 
	audit_log JSON, 
	decided_by VARCHAR(100), 
	reject_reason VARCHAR(500), 
	bridge_ids JSON, 
	created_at TIMESTAMP WITHOUT TIME ZONE NOT NULL, 
	updated_at TIMESTAMP WITHOUT TIME ZONE NOT NULL, 
	expires_at TIMESTAMP WITHOUT TIME ZONE, 
	PRIMARY KEY (id), 
	FOREIGN KEY(source_connection_id) REFERENCES api_connections (id), 
	FOREIGN KEY(target_connection_id) REFERENCES api_connections (id)
);

CREATE INDEX idx_probe_sessions_expires ON probe_sessions (expires_at);

CREATE INDEX idx_probe_sessions_status ON probe_sessions (status);

CREATE TABLE sync_tasks (
	id UUID NOT NULL, 
	mapping_id UUID NOT NULL, 
	task_type VARCHAR(20) NOT NULL, 
	schedule_cron VARCHAR(50), 
	webhook_config JSON, 
	status VARCHAR(20) NOT NULL, 
	sync_mode VARCHAR(20) NOT NULL, 
	fallback_to_async BOOLEAN NOT NULL, 
	auto_threshold INTEGER NOT NULL, 
	last_run_at TIMESTAMP WITHOUT TIME ZONE, 
	next_run_at TIMESTAMP WITHOUT TIME ZONE, 
	last_sync_value VARCHAR(200), 
	depends_on_task_id UUID, 
	conflict_strategy VARCHAR(20), 
	created_at TIMESTAMP WITHOUT TIME ZONE NOT NULL, 
	updated_at TIMESTAMP WITHOUT TIME ZONE NOT NULL, 
	PRIMARY KEY (id), 
	FOREIGN KEY(mapping_id) REFERENCES field_mappings (id), 
	FOREIGN KEY(depends_on_task_id) REFERENCES sync_tasks (id)
);

CREATE TABLE data_buffer (
	id UUID NOT NULL, 
	task_id UUID NOT NULL, 
	source_data JSON NOT NULL, 
	transformed_data JSON, 
	status VARCHAR(20) NOT NULL, 
	error_message TEXT, 
	retry_count INTEGER NOT NULL, 
	data_hash VARCHAR(64), 
	created_at TIMESTAMP WITHOUT TIME ZONE NOT NULL, 
	processed_at TIMESTAMP WITHOUT TIME ZONE, 
	expires_at TIMESTAMP WITHOUT TIME ZONE, 
	PRIMARY KEY (id), 
	FOREIGN KEY(task_id) REFERENCES sync_tasks (id)
);

CREATE INDEX idx_data_buffer_hash ON data_buffer (data_hash);

CREATE INDEX idx_data_buffer_status ON data_buffer (status);

CREATE INDEX idx_data_buffer_task ON data_buffer (task_id);

CREATE TABLE execution_logs (
	id UUID NOT NULL, 
	task_id UUID NOT NULL, 
	buffer_id UUID, 
	action VARCHAR(50) NOT NULL, 
	status VARCHAR(20) NOT NULL, 
	request_data JSON, 
	response_data JSON, 
	error_message TEXT, 
	duration_ms INTEGER, 
	created_at TIMESTAMP WITHOUT TIME ZONE NOT NULL, 
	PRIMARY KEY (id), 
	FOREIGN KEY(task_id) REFERENCES sync_tasks (id), 
	FOREIGN KEY(buffer_id) REFERENCES data_buffer (id)
);

CREATE INDEX idx_execution_logs_created ON execution_logs (created_at);

CREATE INDEX idx_execution_logs_task ON execution_logs (task_id);
