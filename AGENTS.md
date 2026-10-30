# Public package maintenance

Use Python 3.10-compatible code and keep hardware tests offline with fakes.
Never commit credentials, live state, logs, agent transcripts or local .env files.
Installation and imports must not open instruments or start services.
Keep experiment-core independent of ldcon and vendor transports.
Build the wheel and sdist, check their contents, and test the installed wheel
before publishing. Test fixtures are not deployment configurations.
General documentation uses HTML; README.rst is package-index metadata.
