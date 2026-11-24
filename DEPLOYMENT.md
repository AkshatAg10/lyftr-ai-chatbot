# Deployment guide and submission checklist

Plan: backend on **Render** (Docker web service, free), frontend on **Vercel** (free). You need: a GitHub account, a Google account (for Gemini keys), Render and Vercel accounts (sign in with GitHub).

## A. Get two Gemini API keys
1. Go to https://aistudio.google.com/apikey and sign in.
2. Click **Create API key** and copy it somewhere private (a password manager, not the repo, not chat).
3. Create a second key in a **different Google Cloud project** (use "Create API key in new project") or from a second Google account. Keys in the same project share one quota, which would defeat the failover.
4. Your pool is `LLM_API_KEYS=<key1>,<key2>` (no spaces).

If requests fail with "model not found", change `LLM_MODEL` to a currently available Gemini model, e.g. `gemini/gemini-2.5-flash-lite`.

## B. Local run first
```bash
cp .env.example .env     # put your real keys in .env
make up
```
Open http://localhost:3000, send a message, and run the curl checks in README section 4.

## C. Publish to GitHub
1. https://github.com/new -> name it, set **Public**, do NOT add a README/.gitignore -> **Create repository**.
2. In the project root:
```bash
git init
git add .
git status          # confirm .env is NOT listed
git commit -m "LLM chatbot with key failover and caching"
git branch -M main
git remote add origin https://github.com/<you>/<repo>.git
git push -u origin main
```

## D. Deploy the backend on Render
1. https://render.com -> **New +** -> **Web Service** -> connect GitHub, pick the repo.
2. Settings: **Language: Docker**, **Root Directory: `backend`**, **Instance Type: Free**, Dockerfile path `./Dockerfile`.
3. Under **Environment Variables** add:
   - `LLM_API_KEYS` = `<key1>,<key2>`
   - `LLM_MODEL` = `gemini/gemini-2.5-flash`
   - `DATABASE_URL` = `sqlite:////data/app.db`
   - `ALLOWED_ORIGINS` = `http://localhost:3000` (you will update this in step F)
   - `ENABLE_FAILURE_SIMULATION` = `true`
4. Click **Create Web Service**. When it is live, open `https://<name>.onrender.com/health`; it should show `keys_configured: 2`.

Free-tier caveats: the service sleeps after ~15 minutes idle (first request takes about a minute; open `/health` before your demo and before submitting), and the free disk is ephemeral, so SQLite history and the in-memory cache are lost on restart/redeploy. Persistent disks need a paid instance. Persistence across restarts is demonstrated locally via the Docker volume.

## E. Deploy the frontend on Vercel
1. https://vercel.com -> **Add New... -> Project** -> import the repo.
2. **Root Directory: `frontend`**, Framework Preset **Vite** (build `npm run build`, output `dist`).
3. **Environment Variables**: `VITE_API_URL` = your Render URL, e.g. `https://<name>.onrender.com` (no trailing slash).
4. **Deploy**, then copy the URL (`https://<project>.vercel.app`).

## F. Connect them (CORS)
In Render -> your service -> **Environment**, set `ALLOWED_ORIGINS` = `https://<project>.vercel.app` (exact origin, no trailing slash; add `,http://localhost:3000` if you still want local use). Save; Render redeploys.

## G. Test the live URLs (do all before submitting)
- Open the frontend URL in a private window; send two messages; check metadata shows and the second answer remembers the first.
- Send the same first message in a new tab (new session): source `LLM_CACHE`.
- Run the README section 4 curl commands with `BASE` = your Render URL (failover, `*=429` -> 503, 422, history).
- Check the browser console has no CORS errors.
Then fill the URLs into README section 1 and push.

## H. Submission checklist
- [ ] Frontend URL (public, no login): ______
- [ ] Backend URL (public, `/health` works): ______
- [ ] Public GitHub repository URL: ______
- [ ] Demo video, 2-3 min, with voice (deployed app, all 3 endpoints, cache hit, `X-Simulate-Failure` failover): ______
- [ ] `ENABLE_FAILURE_SIMULATION=true` set on the deployed backend
- [ ] README section 1 URLs and section 5 filled in
- [ ] No real keys in the repo (`git log -p | grep -i AIza` returns nothing)
- [ ] Submitted via the Submission Form in the PDF within 24 hours
