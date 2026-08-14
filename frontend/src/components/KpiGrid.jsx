import React from "react";

import { translate } from "../i18n.js";
import { numberFormatter } from "../utils.js";

export function KpiGrid({ summary, language }) {
  const statuses = summary?.by_status || {};
  const pending = (statuses.NEW || 0)
    + (statuses.NEEDS_REVIEW || 0)
    + (statuses.READY_FOR_ACCOUNTING || 0);
  const done = (statuses.ACCOUNTED || 0) + (statuses.CLOSED || 0);
  const cards = [
    [summary?.total || 0, "total"],
    [summary?.by_type?.LOST || 0, "lost"],
    [summary?.by_type?.DAMAGED || 0, "damaged"],
    [pending, "pending"],
    [done, "done"],
    [summary?.warnings || 0, "warning"],
  ];

  return (
    <section className="cards" aria-label={translate(language, "overview")}>
      {cards.map(([value, key]) => (
        <article className="card" key={key}>
          <div className="num">{numberFormatter.format(value)}</div>
          <div className="label">{translate(language, key)}</div>
        </article>
      ))}
    </section>
  );
}
