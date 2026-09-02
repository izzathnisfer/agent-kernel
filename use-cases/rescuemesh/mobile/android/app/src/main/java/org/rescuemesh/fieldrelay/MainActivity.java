package org.rescuemesh.fieldrelay;

import android.annotation.SuppressLint;
import android.app.Activity;
import android.content.Context;
import android.content.SharedPreferences;
import android.graphics.Color;
import android.graphics.Typeface;
import android.graphics.drawable.GradientDrawable;
import android.net.ConnectivityManager;
import android.net.Network;
import android.net.NetworkCapabilities;
import android.os.Bundle;
import android.os.Handler;
import android.os.Looper;
import android.view.Gravity;
import android.view.View;
import android.view.ViewGroup;
import android.widget.ArrayAdapter;
import android.widget.Button;
import android.widget.CheckBox;
import android.widget.EditText;
import android.widget.LinearLayout;
import android.widget.ScrollView;
import android.widget.Spinner;
import android.widget.TextView;
import android.widget.Toast;

import org.json.JSONArray;
import org.json.JSONObject;

import java.io.BufferedReader;
import java.io.InputStream;
import java.io.InputStreamReader;
import java.io.OutputStream;
import java.net.HttpURLConnection;
import java.net.URL;
import java.nio.charset.StandardCharsets;
import java.util.ArrayList;
import java.util.List;
import java.util.Locale;
import java.util.UUID;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;

@SuppressLint("SetTextI18n")
public class MainActivity extends Activity {
    private static final int NAVY = Color.rgb(8, 21, 35);
    private static final int PANEL = Color.rgb(18, 40, 61);
    private static final int TEAL = Color.rgb(85, 225, 232);
    private static final int TEXT = Color.rgb(244, 248, 252);
    private static final int MUTED = Color.rgb(166, 194, 218);
    private static final String PREFS = "rescuemesh_field_relay";
    private static final String OUTBOX = "outbox";

    private final Handler main = new Handler(Looper.getMainLooper());
    private final ExecutorService network = Executors.newSingleThreadExecutor();
    private SharedPreferences prefs;
    private EditText serverInput;
    private TextView connectionText;
    private TextView queueText;
    private TextView boardText;

    private EditText incidentLocation;
    private EditText incidentPeople;
    private EditText incidentVulnerable;
    private EditText incidentContact;
    private EditText incidentNotes;
    private Spinner incidentSeverity;
    private final List<CheckBox> needChecks = new ArrayList<>();

    private EditText resourceProvider;
    private EditText resourceQuantity;
    private EditText resourceLocation;
    private EditText resourceDelay;
    private EditText resourceContact;
    private EditText resourceNotes;
    private Spinner resourceType;

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        prefs = getSharedPreferences(PREFS, MODE_PRIVATE);
        getWindow().setStatusBarColor(NAVY);
        getWindow().setNavigationBarColor(NAVY);

        ScrollView scroll = new ScrollView(this);
        scroll.setBackgroundColor(NAVY);
        LinearLayout root = column(18);
        root.setPadding(dp(18), dp(20), dp(18), dp(40));
        scroll.addView(root);

        TextView eyebrow = text("RESCUEMESH · FIELD RELAY", 12, TEAL, true);
        root.addView(eyebrow);
        TextView title = text("Offline-first disaster reporting", 29, TEXT, true);
        title.setPadding(0, dp(4), 0, dp(4));
        root.addView(title);
        root.addView(text(
                "Residents and volunteers can capture incidents or resource offers even when connectivity drops. " +
                        "Queued reports synchronize to the same RescueMesh coordination ledger when a connection returns.",
                15, MUTED, false));

        root.addView(connectionCard());
        root.addView(incidentCard());
        root.addView(resourceCard());
        root.addView(boardCard());
        root.addView(text(
                "Safety boundary: RescueMesh is decision support, not an emergency number. " +
                        "Resource allocation remains a proposal until a human coordinator confirms it.",
                12, MUTED, false));

        setContentView(scroll);
        updateQueueLabel();
        loadBoard();
    }

    private View connectionCard() {
        LinearLayout card = card();
        card.addView(sectionTitle("Connection & offline outbox"));
        serverInput = input("RescueMesh server URL");
        serverInput.setSingleLine(true);
        serverInput.setText(prefs.getString("server_url", "http://10.0.2.2:8000"));
        card.addView(serverInput);
        card.addView(text("Android emulator: 10.0.2.2. Physical phone: enter this laptop's LAN IP, for example http://192.168.1.5:8000.", 12, MUTED, false));

        LinearLayout row = row();
        Button save = button("Save & test");
        save.setOnClickListener(v -> {
            prefs.edit().putString("server_url", normalizedBase()).apply();
            loadBoard();
        });
        Button sync = button("Sync queued");
        sync.setOnClickListener(v -> syncOutbox(true));
        row.addView(save, weight());
        row.addView(sync, weightWithMargin());
        card.addView(row);

        connectionText = text("Checking server…", 13, MUTED, true);
        connectionText.setPadding(0, dp(10), 0, 0);
        card.addView(connectionText);
        queueText = text("Outbox: 0 queued", 13, TEAL, true);
        card.addView(queueText);
        return card;
    }

    private View incidentCard() {
        LinearLayout card = card();
        card.addView(sectionTitle("Report an incident"));
        card.addView(text("Capture the minimum operational facts. Contact information is optional and never appears on the public command board.", 13, MUTED, false));
        incidentLocation = input("Area / location");
        incidentPeople = input("Approx. people affected");
        incidentPeople.setInputType(android.text.InputType.TYPE_CLASS_NUMBER);
        incidentPeople.setText("1");
        incidentSeverity = spinner(new String[]{"moderate", "low", "high", "critical"});
        incidentVulnerable = input("Vulnerable groups (optional)");
        incidentContact = input("Reporter contact (optional, private)");
        incidentNotes = input("Useful notes (optional)");
        card.addView(incidentLocation);
        card.addView(label("Severity"));
        card.addView(incidentSeverity);
        card.addView(label("Needs"));

        LinearLayout needs = column(0);
        String[] values = {"rescue", "medical", "water", "food", "shelter", "power"};
        for (String value : values) {
            CheckBox box = new CheckBox(this);
            box.setText(value.substring(0, 1).toUpperCase(Locale.ROOT) + value.substring(1));
            box.setTextColor(TEXT);
            box.setButtonTintList(android.content.res.ColorStateList.valueOf(TEAL));
            needChecks.add(box);
            needs.addView(box);
        }
        card.addView(needs);
        card.addView(incidentPeople);
        card.addView(incidentVulnerable);
        card.addView(incidentContact);
        card.addView(incidentNotes);
        Button send = button("Send incident · queue if offline");
        send.setOnClickListener(v -> queueIncident());
        card.addView(send);
        return card;
    }

    private View resourceCard() {
        LinearLayout card = card();
        card.addView(sectionTitle("Offer a community resource"));
        resourceProvider = input("Provider / organisation");
        resourceType = spinner(new String[]{"boat", "ambulance", "vehicle", "drinking water", "food", "shelter", "generator", "medicine", "first aid"});
        resourceQuantity = input("Quantity");
        resourceQuantity.setInputType(android.text.InputType.TYPE_CLASS_NUMBER);
        resourceQuantity.setText("1");
        resourceLocation = input("Resource location / area");
        resourceDelay = input("Available in minutes");
        resourceDelay.setInputType(android.text.InputType.TYPE_CLASS_NUMBER);
        resourceDelay.setText("0");
        resourceContact = input("Contact (optional, private)");
        resourceNotes = input("Notes (optional)");
        card.addView(resourceProvider);
        card.addView(label("Resource type"));
        card.addView(resourceType);
        card.addView(resourceQuantity);
        card.addView(resourceLocation);
        card.addView(resourceDelay);
        card.addView(resourceContact);
        card.addView(resourceNotes);
        Button send = button("Send resource · queue if offline");
        send.setOnClickListener(v -> queueResource());
        card.addView(send);
        return card;
    }

    private View boardCard() {
        LinearLayout card = card();
        card.addView(sectionTitle("Live privacy-safe board"));
        Button refresh = button("Refresh command picture");
        refresh.setOnClickListener(v -> loadBoard());
        card.addView(refresh);
        boardText = text("No board loaded yet.", 14, TEXT, false);
        boardText.setTypeface(Typeface.MONOSPACE);
        boardText.setPadding(0, dp(12), 0, 0);
        card.addView(boardText);
        return card;
    }

    private void queueIncident() {
        String location = incidentLocation.getText().toString().trim();
        StringBuilder needs = new StringBuilder();
        for (CheckBox box : needChecks) {
            if (box.isChecked()) {
                if (needs.length() > 0) needs.append(", ");
                needs.append(box.getText().toString().toLowerCase(Locale.ROOT));
            }
        }
        if (location.isEmpty() || needs.length() == 0) {
            toast("Add a location and at least one need.");
            return;
        }
        try {
            JSONObject payload = new JSONObject();
            payload.put("client_request_id", UUID.randomUUID().toString());
            payload.put("location", location);
            payload.put("needs", needs.toString());
            payload.put("people_count", intValue(incidentPeople, 1));
            payload.put("severity", incidentSeverity.getSelectedItem().toString());
            payload.put("vulnerable_groups", incidentVulnerable.getText().toString().trim());
            payload.put("reporter_contact", incidentContact.getText().toString().trim());
            payload.put("notes", incidentNotes.getText().toString().trim());
            enqueue("/rescuemesh/api/field/incidents", payload);
            incidentNotes.setText("");
            toast("Incident captured in the outbox.");
            syncOutbox(false);
        } catch (Exception e) {
            toast("Could not capture incident: " + e.getMessage());
        }
    }

    private void queueResource() {
        String provider = resourceProvider.getText().toString().trim();
        String location = resourceLocation.getText().toString().trim();
        if (provider.isEmpty() || location.isEmpty()) {
            toast("Add the provider and resource location.");
            return;
        }
        try {
            JSONObject payload = new JSONObject();
            payload.put("client_request_id", UUID.randomUUID().toString());
            payload.put("provider_name", provider);
            payload.put("resource_type", resourceType.getSelectedItem().toString());
            payload.put("quantity", intValue(resourceQuantity, 1));
            payload.put("location", location);
            payload.put("availability_minutes", intValue(resourceDelay, 0));
            payload.put("contact", resourceContact.getText().toString().trim());
            payload.put("notes", resourceNotes.getText().toString().trim());
            enqueue("/rescuemesh/api/field/resources", payload);
            resourceNotes.setText("");
            toast("Resource captured in the outbox.");
            syncOutbox(false);
        } catch (Exception e) {
            toast("Could not capture resource: " + e.getMessage());
        }
    }

    private synchronized void enqueue(String endpoint, JSONObject payload) throws Exception {
        JSONArray items = outbox();
        JSONObject item = new JSONObject();
        item.put("endpoint", endpoint);
        item.put("payload", payload);
        items.put(item);
        saveOutbox(items);
        updateQueueLabel();
    }

    private void syncOutbox(boolean userInitiated) {
        if (!networkAvailable()) {
            setConnection("Offline · reports stay safely queued", false);
            if (userInitiated) toast("No network. Nothing was lost; the outbox is retained.");
            return;
        }
        final String base = normalizedBase();
        prefs.edit().putString("server_url", base).apply();
        setConnection("Syncing…", true);
        network.execute(() -> {
            try {
                JSONArray items = outbox();
                JSONArray remaining = new JSONArray();
                int sent = 0;
                boolean failed = false;
                for (int i = 0; i < items.length(); i++) {
                    JSONObject item = items.getJSONObject(i);
                    if (failed) {
                        remaining.put(item);
                        continue;
                    }
                    try {
                        postJson(base + item.getString("endpoint"), item.getJSONObject("payload"));
                        sent++;
                    } catch (Exception ex) {
                        remaining.put(item);
                        failed = true;
                    }
                }
                saveOutbox(remaining);
                int sentFinal = sent;
                boolean failedFinal = failed;
                main.post(() -> {
                    updateQueueLabel();
                    if (failedFinal) {
                        setConnection("Server unreachable · unsent items remain queued", false);
                    } else {
                        setConnection("Connected · outbox synchronized", true);
                    }
                    if (userInitiated && sentFinal > 0) toast("Synchronized " + sentFinal + " queued item(s).");
                    loadBoard();
                });
            } catch (Exception e) {
                main.post(() -> setConnection("Sync failed · outbox retained", false));
            }
        });
    }

    private void loadBoard() {
        final String base = normalizedBase();
        prefs.edit().putString("server_url", base).apply();
        network.execute(() -> {
            try {
                JSONObject root = new JSONObject(get(base + "/rescuemesh/api/state"));
                String rendered = renderBoard(root);
                main.post(() -> {
                    setConnection("Connected to RescueMesh", true);
                    boardText.setText(rendered);
                    if (outboxLength() > 0) syncOutbox(false);
                });
            } catch (Exception e) {
                main.post(() -> {
                    setConnection("Offline / server unavailable · capture still works", false);
                    boardText.setText("Board unavailable. You can still record incidents and resources; they will sync later.");
                });
            }
        });
    }

    private String renderBoard(JSONObject root) throws Exception {
        JSONObject m = root.getJSONObject("metrics");
        StringBuilder out = new StringBuilder();
        out.append("ACTIVE ").append(m.optInt("incidents_active"))
                .append("   VERIFIED ").append(m.optInt("incidents_verified"))
                .append("\nPEOPLE ").append(m.optInt("people_reported"))
                .append("   RESOURCES ").append(m.optInt("available_resources"))
                .append("\nDUPLICATES MERGED ").append(m.optInt("duplicate_reports_merged"))
                .append("   MATCHES ").append(m.optInt("confirmed_matches"));
        JSONArray incidents = root.getJSONArray("incidents");
        int limit = Math.min(incidents.length(), 6);
        for (int i = 0; i < limit; i++) {
            JSONObject item = incidents.getJSONObject(i);
            out.append("\n\n").append(item.optString("incident_id"))
                    .append(" · ").append(item.optString("priority_band"))
                    .append(item.optBoolean("verified") ? " · VERIFIED" : " · VERIFYING")
                    .append("\n").append(item.optString("location"))
                    .append("\nneeds: ").append(item.optJSONArray("need_tags"));
        }
        return out.toString();
    }

    private String normalizedBase() {
        String raw = serverInput == null ? prefs.getString("server_url", "http://10.0.2.2:8000") : serverInput.getText().toString();
        raw = raw == null ? "" : raw.trim();
        if (raw.isEmpty()) raw = "http://10.0.2.2:8000";
        while (raw.endsWith("/")) raw = raw.substring(0, raw.length() - 1);
        return raw;
    }

    private boolean networkAvailable() {
        ConnectivityManager manager = (ConnectivityManager) getSystemService(Context.CONNECTIVITY_SERVICE);
        if (manager == null) return false;
        Network active = manager.getActiveNetwork();
        if (active == null) return false;
        NetworkCapabilities caps = manager.getNetworkCapabilities(active);
        return caps != null && (caps.hasTransport(NetworkCapabilities.TRANSPORT_WIFI)
                || caps.hasTransport(NetworkCapabilities.TRANSPORT_CELLULAR)
                || caps.hasTransport(NetworkCapabilities.TRANSPORT_ETHERNET));
    }

    private String get(String target) throws Exception {
        HttpURLConnection connection = (HttpURLConnection) new URL(target).openConnection();
        connection.setConnectTimeout(5000);
        connection.setReadTimeout(7000);
        connection.setRequestMethod("GET");
        return readResponse(connection);
    }

    private String postJson(String target, JSONObject payload) throws Exception {
        HttpURLConnection connection = (HttpURLConnection) new URL(target).openConnection();
        connection.setConnectTimeout(5000);
        connection.setReadTimeout(7000);
        connection.setRequestMethod("POST");
        connection.setRequestProperty("Content-Type", "application/json; charset=utf-8");
        connection.setDoOutput(true);
        byte[] body = payload.toString().getBytes(StandardCharsets.UTF_8);
        try (OutputStream output = connection.getOutputStream()) {
            output.write(body);
        }
        return readResponse(connection);
    }

    private String readResponse(HttpURLConnection connection) throws Exception {
        int code = connection.getResponseCode();
        InputStream stream = code >= 200 && code < 300 ? connection.getInputStream() : connection.getErrorStream();
        StringBuilder body = new StringBuilder();
        if (stream != null) {
            try (BufferedReader reader = new BufferedReader(new InputStreamReader(stream, StandardCharsets.UTF_8))) {
                String line;
                while ((line = reader.readLine()) != null) body.append(line);
            }
        }
        connection.disconnect();
        if (code < 200 || code >= 300) throw new IllegalStateException("HTTP " + code + ": " + body);
        return body.toString();
    }

    private synchronized JSONArray outbox() {
        try {
            return new JSONArray(prefs.getString(OUTBOX, "[]"));
        } catch (Exception e) {
            return new JSONArray();
        }
    }

    private synchronized void saveOutbox(JSONArray items) {
        prefs.edit().putString(OUTBOX, items.toString()).apply();
    }

    private int outboxLength() {
        return outbox().length();
    }

    private void updateQueueLabel() {
        if (queueText != null) queueText.setText("Outbox: " + outboxLength() + " queued");
    }

    private void setConnection(String message, boolean ok) {
        if (connectionText != null) {
            connectionText.setText(message);
            connectionText.setTextColor(ok ? TEAL : Color.rgb(255, 193, 99));
        }
    }

    private int intValue(EditText field, int fallback) {
        try {
            return Math.max(0, Integer.parseInt(field.getText().toString().trim()));
        } catch (Exception e) {
            return fallback;
        }
    }

    private LinearLayout card() {
        LinearLayout card = column(10);
        card.setPadding(dp(16), dp(16), dp(16), dp(16));
        GradientDrawable background = new GradientDrawable();
        background.setColor(PANEL);
        background.setCornerRadius(dp(18));
        background.setStroke(dp(1), Color.rgb(42, 75, 103));
        card.setBackground(background);
        LinearLayout.LayoutParams params = new LinearLayout.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.WRAP_CONTENT);
        params.setMargins(0, dp(18), 0, 0);
        card.setLayoutParams(params);
        return card;
    }

    private LinearLayout column(int gap) {
        LinearLayout layout = new LinearLayout(this);
        layout.setOrientation(LinearLayout.VERTICAL);
        if (gap > 0) layout.setShowDividers(LinearLayout.SHOW_DIVIDER_MIDDLE);
        return layout;
    }

    private LinearLayout row() {
        LinearLayout layout = new LinearLayout(this);
        layout.setOrientation(LinearLayout.HORIZONTAL);
        layout.setGravity(Gravity.CENTER_VERTICAL);
        layout.setPadding(0, dp(8), 0, 0);
        return layout;
    }

    private TextView sectionTitle(String value) {
        TextView view = text(value, 20, TEXT, true);
        view.setPadding(0, 0, 0, dp(4));
        return view;
    }

    private TextView label(String value) {
        TextView view = text(value, 12, MUTED, true);
        view.setPadding(0, dp(8), 0, dp(4));
        return view;
    }

    private TextView text(String value, int size, int color, boolean bold) {
        TextView view = new TextView(this);
        view.setText(value);
        view.setTextSize(size);
        view.setTextColor(color);
        view.setLineSpacing(0f, 1.15f);
        if (bold) view.setTypeface(Typeface.DEFAULT, Typeface.BOLD);
        return view;
    }

    private EditText input(String hint) {
        EditText field = new EditText(this);
        field.setHint(hint);
        field.setHintTextColor(Color.rgb(116, 150, 180));
        field.setTextColor(TEXT);
        field.setTextSize(15);
        field.setSingleLine(false);
        field.setPadding(dp(12), dp(10), dp(12), dp(10));
        GradientDrawable background = new GradientDrawable();
        background.setColor(Color.rgb(10, 28, 44));
        background.setCornerRadius(dp(10));
        background.setStroke(dp(1), Color.rgb(44, 78, 106));
        field.setBackground(background);
        LinearLayout.LayoutParams params = new LinearLayout.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.WRAP_CONTENT);
        params.setMargins(0, dp(8), 0, 0);
        field.setLayoutParams(params);
        return field;
    }

    private Spinner spinner(String[] values) {
        Spinner spinner = new Spinner(this);
        ArrayAdapter<String> adapter = new ArrayAdapter<String>(this, android.R.layout.simple_spinner_dropdown_item, values) {
            @Override
            public View getView(int position, View convertView, ViewGroup parent) {
                TextView view = (TextView) super.getView(position, convertView, parent);
                view.setTextColor(TEXT);
                view.setTextSize(15);
                view.setPadding(dp(12), dp(10), dp(12), dp(10));
                return view;
            }
        };
        spinner.setAdapter(adapter);
        GradientDrawable background = new GradientDrawable();
        background.setColor(Color.rgb(10, 28, 44));
        background.setCornerRadius(dp(10));
        background.setStroke(dp(1), Color.rgb(44, 78, 106));
        spinner.setBackground(background);
        return spinner;
    }

    private Button button(String label) {
        Button button = new Button(this);
        button.setText(label);
        button.setTextColor(NAVY);
        button.setTextSize(13);
        button.setTypeface(Typeface.DEFAULT, Typeface.BOLD);
        button.setAllCaps(false);
        GradientDrawable background = new GradientDrawable();
        background.setColor(TEAL);
        background.setCornerRadius(dp(12));
        button.setBackground(background);
        button.setPadding(dp(12), dp(9), dp(12), dp(9));
        return button;
    }

    private LinearLayout.LayoutParams weight() {
        return new LinearLayout.LayoutParams(0, ViewGroup.LayoutParams.WRAP_CONTENT, 1f);
    }

    private LinearLayout.LayoutParams weightWithMargin() {
        LinearLayout.LayoutParams params = weight();
        params.setMargins(dp(8), 0, 0, 0);
        return params;
    }

    private int dp(int value) {
        return (int) (value * getResources().getDisplayMetrics().density + 0.5f);
    }

    private void toast(String message) {
        Toast.makeText(this, message, Toast.LENGTH_SHORT).show();
    }

    @Override
    protected void onDestroy() {
        super.onDestroy();
        network.shutdownNow();
    }
}
