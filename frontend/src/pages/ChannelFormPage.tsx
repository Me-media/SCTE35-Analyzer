import { useEffect, useState, type FormEvent } from "react";
import { useTranslation } from "react-i18next";
import { useNavigate, useParams } from "react-router-dom";
import { api } from "../api/client";
import { PasswordField } from "../components/PasswordField";
import type { Channel, ChannelInput, ProcessingMode, SystemSettings, TranscodeTemplate, TransportType } from "../types";

const DEFAULT_CHANNEL: ChannelInput = {
  name: "",
  input_type: "srt_listener",
  input_srt_host: null,
  input_srt_port: 9000,
  input_multicast_address: null,
  input_multicast_port: null,
  input_srt_latency_ms: 120,
  input_srt_passphrase: null,
  input_srt_stream_id: null,
  output_type: "srt_listener",
  output_srt_host: null,
  output_srt_port: 9001,
  output_multicast_address: null,
  output_multicast_port: null,
  output_multicast_ttl: 32,
  output_multicast_dscp: null,
  output_srt_latency_ms: 120,
  output_srt_passphrase: null,
  output_srt_stream_id: null,
  processing_mode: "passthrough",
  template_id: null,
  metadata_passthrough: false,
  scte35_pid: null,
};

export function ChannelFormPage() {
  const { t } = useTranslation();
  const navigate = useNavigate();
  const { id } = useParams();
  const isNew = !id || id === "new";

  const [channel, setChannel] = useState<ChannelInput>(DEFAULT_CHANNEL);
  const [templates, setTemplates] = useState<TranscodeTemplate[]>([]);
  const [settings, setSettings] = useState<SystemSettings | null>(null);
  const [error, setError] = useState<string | null>(null);
  // Spec: while transcoding is disabled, the choice to select/create a
  // transcode configuration must be removed from the UI entirely - not
  // just rejected on submit. An existing channel already configured for
  // transcode keeps that setting (and its template picker) visible so it
  // can still be viewed/edited here; it's the *new selection* that's gone.
  const transcodingAllowed = settings === null || settings.transcoding_enabled || channel.processing_mode === "transcode";

  useEffect(() => {
    api.get<TranscodeTemplate[]>("/templates").then(setTemplates);
    api.get<SystemSettings>("/system-settings").then(setSettings).catch(() => setSettings(null));
  }, []);

  useEffect(() => {
    if (!isNew && id) {
      api.get<Channel>(`/channels/${id}`).then(setChannel);
    }
  }, [id, isNew]);

  function set<K extends keyof ChannelInput>(key: K, value: ChannelInput[K]) {
    setChannel((c) => ({ ...c, [key]: value }));
  }

  async function onSubmit(e: FormEvent) {
    e.preventDefault();
    setError(null);
    try {
      if (isNew) await api.post("/channels", channel);
      else await api.put(`/channels/${id}`, channel);
      navigate("/channels");
    } catch (err) {
      setError(String(err));
    }
  }

  return (
    <div className="mx-auto max-w-3xl p-6">
      <h1 className="mb-4 text-xl font-semibold">{isNew ? t("common.create") : t("common.edit")}</h1>
      <form onSubmit={onSubmit} className="space-y-6">
        <div>
          <label className="mb-1 block text-sm">{t("common.name")}</label>
          <input
            required
            value={channel.name}
            onChange={(e) => set("name", e.target.value)}
            className="w-full max-w-md rounded border px-3 py-2"
            style={{ borderColor: "var(--border)", background: "var(--bg-raised)" }}
          />
        </div>

        <TransportSection
          title={t("channel.input")}
          type={channel.input_type}
          onType={(v) => set("input_type", v)}
          host={channel.input_srt_host}
          onHost={(v) => set("input_srt_host", v)}
          port={channel.input_srt_port}
          onPort={(v) => set("input_srt_port", v)}
          mcAddress={channel.input_multicast_address}
          onMcAddress={(v) => set("input_multicast_address", v)}
          mcPort={channel.input_multicast_port}
          onMcPort={(v) => set("input_multicast_port", v)}
          latency={channel.input_srt_latency_ms}
          onLatency={(v) => set("input_srt_latency_ms", v)}
          passphrase={channel.input_srt_passphrase}
          onPassphrase={(v) => set("input_srt_passphrase", v)}
          streamId={channel.input_srt_stream_id}
          onStreamId={(v) => set("input_srt_stream_id", v)}
          allowMulticast
        />

        <TransportSection
          title={t("channel.output")}
          type={channel.output_type}
          onType={(v) => set("output_type", v)}
          host={channel.output_srt_host}
          onHost={(v) => set("output_srt_host", v)}
          port={channel.output_srt_port}
          onPort={(v) => set("output_srt_port", v)}
          mcAddress={channel.output_multicast_address}
          onMcAddress={(v) => set("output_multicast_address", v)}
          mcPort={channel.output_multicast_port}
          onMcPort={(v) => set("output_multicast_port", v)}
          latency={channel.output_srt_latency_ms}
          onLatency={(v) => set("output_srt_latency_ms", v)}
          passphrase={channel.output_srt_passphrase}
          onPassphrase={(v) => set("output_srt_passphrase", v)}
          streamId={channel.output_srt_stream_id}
          onStreamId={(v) => set("output_srt_stream_id", v)}
          ttl={channel.output_multicast_ttl}
          onTtl={(v) => set("output_multicast_ttl", v)}
          dscp={channel.output_multicast_dscp}
          onDscp={(v) => set("output_multicast_dscp", v)}
          allowMulticast
        />

        <div>
          <label className="mb-1 block text-sm">{t("channel.processing")}</label>
          {transcodingAllowed ? (
            <div className="flex items-center gap-3">
              <select
                value={channel.processing_mode}
                onChange={(e) => set("processing_mode", e.target.value as ProcessingMode)}
                className="rounded border px-3 py-2"
                style={{ borderColor: "var(--border)", background: "var(--bg-raised)" }}
              >
                <option value="passthrough">{t("channel.processingMode.passthrough")}</option>
                <option value="transcode">{t("channel.processingMode.transcode")}</option>
              </select>
              {channel.processing_mode === "transcode" && (
                <select
                  required
                  value={channel.template_id ?? ""}
                  onChange={(e) => set("template_id", e.target.value || null)}
                  className="rounded border px-3 py-2"
                  style={{ borderColor: "var(--border)", background: "var(--bg-raised)" }}
                >
                  <option value="" disabled>
                    {t("channel.fields.template")}
                  </option>
                  {templates.map((tpl) => (
                    <option key={tpl.id} value={tpl.id}>
                      {tpl.name}
                    </option>
                  ))}
                </select>
              )}
            </div>
          ) : (
            // Transcoding is off system-wide and this channel isn't
            // already in transcode mode (that case is covered by
            // transcodingAllowed above, keeping the full picker so an
            // existing transcode channel can still be viewed/switched
            // away) - so there is no real choice to offer here at all,
            // not even a single-option dropdown. channel.processing_mode
            // is already "passthrough" in this branch (the only other
            // enum value is excluded by transcodingAllowed's own check).
            <p className="text-sm" style={{ color: "var(--text-muted)" }}>
              {t("channel.processingMode.passthrough")}
              {" — "}
              {t("channel.transcodingDisabledHint")}
            </p>
          )}
        </div>

        {error && <p className="text-sm text-red-400">{error}</p>}

        <div className="flex gap-2">
          <button type="submit" className="rounded bg-gold-500 px-4 py-2 font-medium text-black hover:bg-gold-400">
            {t("common.save")}
          </button>
          <button
            type="button"
            onClick={() => navigate("/channels")}
            className="rounded border px-4 py-2"
            style={{ borderColor: "var(--border)" }}
          >
            {t("common.cancel")}
          </button>
        </div>
      </form>
    </div>
  );
}

interface TransportSectionProps {
  title: string;
  type: TransportType;
  onType: (v: TransportType) => void;
  host: string | null;
  onHost: (v: string | null) => void;
  port: number | null;
  onPort: (v: number | null) => void;
  mcAddress: string | null;
  onMcAddress: (v: string | null) => void;
  mcPort: number | null;
  onMcPort: (v: number | null) => void;
  latency: number;
  onLatency: (v: number) => void;
  passphrase: string | null;
  onPassphrase: (v: string | null) => void;
  streamId: string | null;
  onStreamId: (v: string | null) => void;
  ttl?: number;
  onTtl?: (v: number) => void;
  dscp?: number | null;
  onDscp?: (v: number | null) => void;
  allowMulticast?: boolean;
}

function TransportSection(props: TransportSectionProps) {
  const { t } = useTranslation();
  const inputStyle = { borderColor: "var(--border)", background: "var(--bg-raised)" };

  return (
    <fieldset className="rounded-lg border p-4" style={{ borderColor: "var(--border)" }}>
      <legend className="px-1 text-sm font-medium">{props.title}</legend>

      <div className="mb-3">
        <select
          value={props.type}
          onChange={(e) => props.onType(e.target.value as TransportType)}
          className="rounded border px-3 py-2"
          style={inputStyle}
        >
          {(["srt_caller", "srt_listener", "multicast"] as TransportType[])
            .filter((tt) => props.allowMulticast || tt !== "multicast")
            .map((tt) => (
              <option key={tt} value={tt}>
                {t(`channel.transportType.${tt}`)}
              </option>
            ))}
        </select>
      </div>

      {(props.type === "srt_caller" || props.type === "srt_listener") && (
        <div className="mb-3 grid grid-cols-2 gap-3">
          {props.type === "srt_caller" && (
            <div>
              <label className="mb-1 block text-xs">{t("channel.fields.host")}</label>
              <input
                required
                value={props.host ?? ""}
                onChange={(e) => props.onHost(e.target.value)}
                className="w-full rounded border px-3 py-2"
                style={inputStyle}
              />
            </div>
          )}
          <div>
            <label className="mb-1 block text-xs">{t("channel.fields.port")}</label>
            <input
              required
              type="number"
              min={1}
              max={65535}
              value={props.port ?? ""}
              onChange={(e) => props.onPort(Number(e.target.value))}
              className="w-full rounded border px-3 py-2"
              style={inputStyle}
            />
          </div>
          <div>
            <label className="mb-1 block text-xs">{t("channel.fields.latency")}</label>
            <input
              type="number"
              min={20}
              max={8000}
              value={props.latency}
              onChange={(e) => props.onLatency(Number(e.target.value))}
              className="w-full rounded border px-3 py-2"
              style={inputStyle}
            />
          </div>
        </div>
      )}

      {props.type === "multicast" && (
        <div className="mb-3 grid grid-cols-2 gap-3">
          <div>
            <label className="mb-1 block text-xs">{t("channel.fields.multicastAddress")}</label>
            <input
              required
              value={props.mcAddress ?? ""}
              onChange={(e) => props.onMcAddress(e.target.value)}
              placeholder="239.1.1.1"
              className="w-full rounded border px-3 py-2"
              style={inputStyle}
            />
          </div>
          <div>
            <label className="mb-1 block text-xs">{t("channel.fields.multicastPort")}</label>
            <input
              required
              type="number"
              min={1}
              max={65535}
              value={props.mcPort ?? ""}
              onChange={(e) => props.onMcPort(Number(e.target.value))}
              className="w-full rounded border px-3 py-2"
              style={inputStyle}
            />
          </div>
          {props.onTtl && (
            <div>
              <label className="mb-1 block text-xs">{t("channel.fields.ttl")}</label>
              <input
                type="number"
                min={1}
                max={255}
                value={props.ttl}
                onChange={(e) => props.onTtl!(Number(e.target.value))}
                className="w-full rounded border px-3 py-2"
                style={inputStyle}
              />
            </div>
          )}
          {props.onDscp && (
            <div>
              <label className="mb-1 block text-xs">{t("channel.fields.dscp")}</label>
              <input
                type="number"
                min={0}
                max={63}
                value={props.dscp ?? ""}
                onChange={(e) => props.onDscp!(e.target.value === "" ? null : Number(e.target.value))}
                className="w-full rounded border px-3 py-2"
                style={inputStyle}
              />
            </div>
          )}
        </div>
      )}

      {(props.type === "srt_caller" || props.type === "srt_listener") && (
        <div className="rounded border border-dashed p-3" style={{ borderColor: "var(--border)" }}>
          <p className="mb-2 text-xs font-medium">{t("channel.auth.title")}</p>
          <p className="mb-2 text-xs" style={{ color: "var(--text-muted)" }}>
            {t("channel.auth.hint")}
          </p>
          <div className="grid grid-cols-2 gap-3">
            <div>
              <label className="mb-1 block text-xs">{t("channel.auth.passphrase")}</label>
              <PasswordField
                minLength={10}
                maxLength={79}
                value={props.passphrase ?? ""}
                onChange={(v) => props.onPassphrase(v || null)}
                className="w-full rounded border px-3 py-2"
                style={inputStyle}
              />
            </div>
            <div>
              <label className="mb-1 block text-xs">{t("channel.auth.streamId")}</label>
              <input
                value={props.streamId ?? ""}
                onChange={(e) => props.onStreamId(e.target.value || null)}
                className="w-full rounded border px-3 py-2"
                style={inputStyle}
              />
            </div>
          </div>
        </div>
      )}
    </fieldset>
  );
}
