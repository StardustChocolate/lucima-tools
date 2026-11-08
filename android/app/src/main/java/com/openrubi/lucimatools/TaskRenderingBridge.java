package com.openrubi.lucimatools;

import android.view.View;
import android.webkit.JavascriptInterface;
import android.webkit.WebView;

/** Compatibility bridge for shells that previously forced software drawing. */
public final class TaskRenderingBridge {
    private final WebView webView;
    private volatile boolean taskMode;

    public TaskRenderingBridge(WebView webView) {
        this.webView = webView;
    }

    @JavascriptInterface
    public void setTaskMode(boolean enabled) {
        taskMode = enabled;
        webView.post(() -> {
            // Rapid navigation must not apply an older mode after a newer one.
            if (taskMode != enabled) return;
            int layer = View.LAYER_TYPE_NONE;
            if (webView.getLayerType() != layer) {
                webView.setLayerType(layer, null);
                webView.invalidate();
            }
        });
    }
}
