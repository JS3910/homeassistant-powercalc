import { LitElement, css, html, nothing } from "lit";
import { customElement, property } from "lit/decorators.js";
import type { MeasurementRequest } from "../../types";
import { emit } from "../../utils/events";
import { sharedStyles } from "../../styles";

/**
 * Persistent setup banner for refine (extend) runs. Hidden seed fields also live
 * in the setup form so estimate/submit see them; the controller still reattaches
 * resume_policy / seed_session_id / remeasure_existing if the form rebuilds.
 */
@customElement("measure-refine-banner")
export class RefineBanner extends LitElement {
  @property({ attribute: false })
  request?: MeasurementRequest;

  static readonly styles = [
    sharedStyles,
    css`
      :host {
        display: block;
        margin-bottom: 1rem;
      }
      .banner {
        display: grid;
        gap: 0.75rem;
      }
      .banner p {
        margin: 0;
        color: var(--ink);
        line-height: 1.45;
      }
      label.remeasure {
        display: flex;
        align-items: center;
        gap: 0.6rem;
        color: var(--ink);
        font-size: 0.88rem;
        font-weight: 650;
      }
      label.remeasure input {
        width: 1.05rem;
        min-width: 1.05rem;
        min-height: 1.05rem;
        margin: 0;
        accent-color: var(--signal);
      }
    `,
  ];

  render() {
    const request = this.request;
    if (request?.resume_policy !== "extend" || !request.seed_session_id) return nothing;
    const source = request.product_name || request.model_id || "the previous session";
    return html`
      <div class="notice banner" role="status">
        <p>This run keeps existing points from ${source}. Only missing points will be measured.</p>
        <label class="remeasure">
          <input
            type="checkbox"
            name="remeasure_existing"
            .checked=${Boolean(request.remeasure_existing)}
            @change=${this.onRemeasureChange}
          />
          <span>Re-measure existing points</span>
        </label>
        <input type="hidden" name="resume_policy" value="extend" />
        <input type="hidden" name="seed_session_id" value=${request.seed_session_id} />
      </div>
    `;
  }

  private onRemeasureChange(event: Event): void {
    const input = event.target as HTMLInputElement;
    emit(this, "remeasure-change", input.checked);
  }
}
