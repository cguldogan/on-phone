FROM alpine:3.22
RUN apk add --no-cache asterisk asterisk-sounds-en asterisk-sounds-moh asterisk-opus asterisk-srtp asterisk-sample-config tzdata
CMD ["asterisk", "-f", "-U", "asterisk", "-G", "asterisk", "-vvv"]
