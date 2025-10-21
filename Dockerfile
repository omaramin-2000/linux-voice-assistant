FROM python:3.13-slim-trixie
          
ENV LANG=C.UTF-8
ENV DEBIAN_FRONTEND=noninteractive
ENV PYTHONUNBUFFERED=1
          
RUN apt-get update && \
    apt-get install --yes --no-install-recommends \
    avahi-utils alsa-utils libportaudio2 portaudio19-dev \
    build-essential libmpv-dev pulseaudio
          
WORKDIR /srv
COPY . ./
RUN ./script/setup
          
ENTRYPOINT ["./script/run"]
CMD ["--host", "0.0.0.0", "--port", "6053", "--debug"]
