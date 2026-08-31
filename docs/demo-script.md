# Short live demo flow

The live demonstration uses the public Cloud Run application, one paired tenant chat, and one paired Vendor B group. Vendor A is a deterministic timeout so the fallback is repeatable.

## Before recording

- Open the public `.run.app` URL and confirm it shows **Waiting for a real tenant report**.
- Send `/start` in the paired tenant chat and Vendor B group.
- Prepare one leak photo and one after-repair photo.
- Use Telegram's **Reply** action for every vendor price, ETA, photo, and summary prompt.

## Steps

1. In the tenant chat, send `/report`.
2. Send a safe leak description, one photo, and a voice note. Tap **✅ Submit report** on the newest draft summary.
3. On the website, show the tenant evidence, Gemini assessment, containment instructions, S$250 work order, and persisted timeline.
4. Wait for Vendor A's 8 or 12 second deadline. Show the automatic Vendor B fallback.
5. In the Vendor B group:
   - Tap **Accept**.
   - Reply `220` to the quote prompt.
   - Tap **Confirm S$220.00**.
   - Reply `20` to the ETA prompt.
   - Tap **Confirm 20 minutes**.
   - Tap **Submit quote and ETA**.
6. Show the tenant's automatic vendor and ETA updates.
7. In the Vendor B group:
   - Tap **Start job**.
   - Tap **Prepare completion**.
   - Reply to the photo prompt with the after-repair photo.
   - Reply to the summary prompt with a 10 to 500 character repair summary.
   - Tap **Confirm S$220.00**.
   - Tap **Submit completion**.
8. On the website, show the completion photo, final price, evidence result, and delayed tenant-confirmation task.
9. In the tenant chat, tap **Dry now**.
10. On the website, show the final `CLOSED` state and Cloud Run, Gemini 3.5 Flash, and Firestore proof.

## Important behavior

- The public console does not expose unfinished Telegram drafts. Tenant evidence appears after submission.
- The agent does not invent vendor acceptance, quote, ETA, work start, completion, or tenant confirmation.
- A late Vendor A response cannot replace Vendor B.
- Prices above S$250 require approval.
- **Still leaking** reopens the same incident during its warranty window.
- Local deterministic replay is only for development and judging fallback. It is hidden in the Cloud Run deployment.
